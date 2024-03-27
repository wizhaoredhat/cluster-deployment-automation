from dataclasses import dataclass
import itertools
import time
import os
import json
from typing import Optional
import requests
from ailib import AssistedClient
import common
from logger import logger
import sys


@dataclass
class AssistedClientClusterInfo:
    id: str
    api_vip: str


@dataclass
class AssistedClientHostInfo:
    status: str
    inventory: str


class AssistedClientAutomation(AssistedClient):  # type: ignore
    def __init__(self, url: str):
        super().__init__(url, quiet=True, debug=False)

    def cluster_exists(self, name: str) -> bool:
        return any(name == x["name"] for x in self.list_clusters())

    def ensure_cluster_deleted(self, name: str) -> None:
        logger.info(f"Ensuring that cluster {name} is not present")
        while self.cluster_exists(name):
            try:
                self.delete_cluster(name)
            except Exception:
                logger.info("failed to delete cluster, will retry..")
            time.sleep(5)

    def ensure_infraenv_created(self, name: str, cfg: dict[str, str]) -> None:
        if name not in (x["name"] for x in self.list_infra_envs()):
            logger.info(f"Creating infraenv {name}")
            self.create_infra_env(name, cfg)

    def ensure_infraenv_deleted(self, name: str) -> None:
        if name in (x["name"] for x in self.list_infra_envs()):
            self.delete_infra_env(name)

    @staticmethod
    def delete_kubeconfig_and_secrets(name: str, kubeconfig_path: Optional[str]) -> None:

        path, kubeconfig_path, downloaded_kubeconfig_path, downloaded_kubeadminpassword_path = common.kubeconfig_get_paths(name, kubeconfig_path)

        try:
            os.remove(kubeconfig_path)
        except OSError:
            pass

        try:
            os.remove(downloaded_kubeadminpassword_path)
        except OSError:
            pass

    def download_kubeconfig_and_secrets(
        self,
        name: str,
        kubeconfig_path: Optional[str],
        *,
        log: bool = True,
    ) -> tuple[str, str]:

        path, kubeconfig_path, downloaded_kubeconfig_path, downloaded_kubeadminpassword_path = common.kubeconfig_get_paths(name, kubeconfig_path)

        self.download_kubeconfig(name, path)
        self.download_kubeadminpassword(name, path)

        if downloaded_kubeconfig_path != kubeconfig_path:
            # download_kubeconfig() does not support specifying the full path.
            # The caller requested another name. Rename.
            os.rename(downloaded_kubeconfig_path, kubeconfig_path)

        if log:
            logger.info(f"KUBECONFIG={kubeconfig_path}")
            logger.info(f"KUBEADMIN_PASSWD={downloaded_kubeadminpassword_path}")

        return kubeconfig_path, downloaded_kubeadminpassword_path

    def download_iso_with_retry(self, infra_env: str, path: str = os.getcwd()) -> None:
        logger.info(self.info_iso(infra_env, {}))
        retries, timeout = 25, 30
        logger.info(f"Download iso from {infra_env} to {path}, retrying for {retries * timeout}s")
        for _ in range(retries):
            try:
                self.download_iso(infra_env, path)
                break
            except Exception:
                time.sleep(timeout)
        else:
            logger.error(f"Failed to download the ISO after {retries} attempts")

    def wait_cluster_ready(self, cluster_name: str) -> None:
        logger.info("Waiting for cluster state to be ready")
        cur_state = None
        while True:
            new_state = self.cluster_state(cluster_name)
            if new_state != cur_state:
                logger.info(f"Cluster state changed to {new_state}")
            cur_state = new_state
            if cur_state == "ready":
                break
            time.sleep(10)

    def cluster_state(self, cluster_name: str) -> str:
        matching_clusters = [x for x in self.list_clusters() if x["name"] == cluster_name]
        if len(matching_clusters) == 0:
            logger.error(f"Requested status of cluster '{cluster_name}' but couldn't find it")
            sys.exit(-1)
        elif len(matching_clusters) > 1:
            logger.error(f"Unexpected number of matching clusters: {matching_clusters}")
            sys.exit(-1)
        elif "status" not in matching_clusters[0]:
            logger.error(f"Status is missing for cluster {cluster_name}")
            sys.exit(-1)
        else:
            s = matching_clusters[0]["status"]
            if isinstance(s, str):
                return s
            else:
                logger.error(f"Unexpected status: {s}")
                sys.exit(-1)

    def start_until_success(self, cluster_name: str) -> None:
        self.wait_cluster_ready(cluster_name)
        logger.info(f"Starting cluster {cluster_name} (will retry until success)")
        for tries in itertools.count(0):
            try:
                self.start_cluster(cluster_name)
            except Exception:
                pass

            cluster = list(filter(lambda e: e["name"] == cluster_name, self.list_clusters()))
            status = cluster[0]["status"]

            if status == "installing":
                logger.info(f"Cluster {cluster_name} is in state installing")
                break
            time.sleep(10)
        logger.info(f"Took {tries} tries to start cluster {cluster_name}")

    def get_ai_host(self, name: str) -> Optional[AssistedClientHostInfo]:
        for h in filter(lambda x: "inventory" in x, self.list_hosts()):
            rhn = h["requested_hostname"]
            if rhn == name:
                return AssistedClientHostInfo(h["status"], h["inventory"])
        return None

    def get_ai_ip(self, name: str, ip_range: tuple[str, str]) -> Optional[str]:
        ai_host = self.get_ai_host(name)
        if ai_host:
            inventory = json.loads(ai_host.inventory)
            routes = inventory["routes"]

            default_nics = [x['interface'] for x in routes if x['destination'] == '0.0.0.0']
            for default_nic in default_nics:
                nic_info = next(nic for nic in inventory.get('interfaces') if nic["name"] == default_nic)
                addr = str(nic_info['ipv4_addresses'][0].split('/')[0])
                if common.ip_range_contains(ip_range, addr):
                    return addr
        return None

    def allow_add_workers(self, cluster_name: str) -> None:
        uuid = self.get_ai_cluster_info(cluster_name).id
        requests.post(f"http://{self.url}/api/assisted-install/v2/clusters/{uuid}/actions/allow-add-workers")

    def get_ai_cluster_info(self, cluster_name: str) -> AssistedClientClusterInfo:
        cluster_info = self.info_cluster(cluster_name)
        if not hasattr(cluster_info, "id"):
            logger.error(f"ID is missing in cluster info for cluster {cluster_name}")
            sys.exit(-1)
        if not hasattr(cluster_info, "api_vips"):
            logger.error(f"Missing api_vips in cluster info for cluster {cluster_name}")
            sys.exit(-1)

        if len(cluster_info.api_vips) == 0:
            logger.error(f"Missing api vip in cluster info for cluster {cluster_name}")
            sys.exit(-1)

        return AssistedClientClusterInfo(cluster_info.id, cluster_info.api_vips[0].ip)
