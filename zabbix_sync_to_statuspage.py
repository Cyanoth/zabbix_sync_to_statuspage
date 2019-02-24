import argparse
import json
import logging
import time
import traceback
from enum import Enum
import requests
import yaml

LOGGING_FORMAT='%(asctime)s %(levelname)-8s %(message)s'
LOGGING_DATETIME_FORMAT='%Y-%m-%d %H:%M:%S'
DRY_RUN = False


# Enum for Zabbix Trigger Constants
class ZbxStatus(Enum):
    operational = 0
    warning = 2
    average = 3
    high = 4
    disaster = 5


# Map Zabbix Trigger Status to Statuspage Severity.
ZBX_SP_MAPPING = {
    "operational": "operational",
    "warning":     "degraded_performance",
    "average":     "partial_outage",
    "high":        "major_outage",
    "disaster":    "major_outage"
}


class ZabbixServiceInfo:
    def __init__(self, service_id, service_name, service_status, is_group_parent=False, linked_parent_id=0):
        self.service_id = service_id
        self.service_name = service_name
        self.service_status = service_status
        self.is_group_parent = is_group_parent
        self.linked_parent_id = linked_parent_id


class StatusPageComponentInfo:
    def __init__(self, component_id, component_name, component_status, is_group, matched=False):
        self.component_id = component_id
        self.component_name = component_name
        self.component_status = component_status
        self.is_group = is_group
        self.matched = matched  # Found matching Zbx<->SP


class ZabbixService:
    def __init__(self, api_host, api_username, api_password):
        self.zabbix_api_url = api_host + "/api_jsonrpc.php"
        self.api_username = api_username
        self.api_password = api_password
        self.session_key = None
        self._authenticate(self.api_username, self.api_password)

    def _authenticate(self, username, password):
        """
        Authenticate to Zabbix using username & password. Obtain a session key
        which persists for as long as was set in Zabbix Administrator Settings.
        :param username: Username to authenticate to a Zabbix Instance
        :param password: Password to authenticate to a Zabbix Instance
        """
        try:
            payload = {"jsonrpc": "2.0", "method": "user.login", "params": {"user": username, "password": password}, "id": 1}
            logging.debug("Authenticating to Zabbix.")
            response = requests.post(self.zabbix_api_url, data=json.dumps(payload), headers={'Content-Type': 'application/json'}, timeout=10)
            self.session_key = response.json()["result"]
            assert self.session_key is not None  # Ensure the session key is set.
            logging.info("Authentication to Zabbix was successful. Session key obtained")
        except Exception as err:
            raise Exception("FATAL: Failed to authenticate to zabbix.\nException: {}".format(err))

    def get_services(self, root_service_id, retry=False):
        """
        Gets Services under a root node with Zabbix API. Extracts information about each service and puts into
        a list of objects.
        :param root_service_id: Zabbix ID of a root service. Entries under this root node are considered for statuspage
        :param retry: Recursion Management. Whether a call to this function was made due to a retry
        :return: List of ZabbixServiceInfo objects
        """
        logging.debug("Querying for Zabbix Services")

        payload = {"jsonrpc": "2.0", "method": "service.get", "params": {"selectDependencies": "extend"},  "id": 1, "auth": self.session_key}
        res = requests.get(self.zabbix_api_url, data=json.dumps(payload), headers={'Content-Type': 'application/json'}, timeout=10)

        if res.status_code == 200:
            zbx_services = res.json()["result"]
        elif (res.status_code == 401 or res.status_code == 403) and not retry:
            logging.info("Query zabbix services response was {}. The session key may have expired. "
                         "Attempting to reauthenticate and trying again.".format(res.status_code))
            self._authenticate(self.api_username, self.api_password)
            return self.get_services(root_service_id, retry=True)  # Retry with a reauthentication attempt
        else:
            res.raise_for_status()

        # Find root service, extract ID & match to their object. Any components/groups under this root are to sync.
        root_children = list(filter(lambda root_child: root_child['serviceid'] == str(root_service_id), zbx_services))
        root_children_id = [child["serviceid"] for child in root_children[0]["dependencies"]]
        root_children = list(filter(lambda root_child: root_child["serviceid"] in root_children_id, zbx_services))

        # Find Services under the root & Service Groups under the root
        root_services = list(filter(lambda root_service: len(root_service["dependencies"]) == 0, root_children))
        root_groups = list(filter(lambda root_group: len(root_group["dependencies"]) > 0, root_children))

        zbx_info = []  # Hold list of Zabbix Service Information objects

        for rc in root_services: # Root Components
            logging.debug("Found a service with no children. ID: {} Name: {}".format(rc["serviceid"], rc["name"]))
            zbx_info.append(ZabbixServiceInfo(rc["serviceid"], rc["name"], ZbxStatus(int(rc["status"])).name))

        for rg in root_groups: # Root Groups
            logging.debug("Found a service group with ID: {} Name: {}".format(rg["serviceid"], rg["name"]))
            zbx_info.append(ZabbixServiceInfo(rg["serviceid"], rg["name"], ZbxStatus(int(rg["status"])).name, is_group_parent=True))

            group_children = list(filter(lambda group_child: group_child['serviceid'] == str(rg["serviceid"]), zbx_services))
            group_children_id = [child["serviceid"] for child in group_children[0]["dependencies"]]
            group_children = list(filter(lambda group_child: group_child["serviceid"] in group_children_id, zbx_services))

            # Find the components which are under this group
            for gc in group_children: # Group Child
                if len(gc["dependencies"]) == 0:  # Ensure this component has no further children
                    logging.debug("Found child service named {} with id: {} from parent named: {} with id: {}. "
                                  "It has no further descendants".format(gc["name"], gc["serviceid"], rg["name"], rg["serviceid"]))
                else:
                    logging.debug("Found child service named {} with id: {} from parent named: {} with id: {}. "
                                  "But it has further descendants which is not allowed. (Statuspage does not have nested groups)"
                                  "Descendants will not be sync'd.".format(gc["name"], gc["serviceid"], rg["name"], rg["serviceid"]))

                zbx_info.append(ZabbixServiceInfo(gc["serviceid"], gc["name"], ZbxStatus(int(gc["status"])).name, linked_parent_id=rg["serviceid"]))

        return zbx_info


class StatusPageSync:
    def __init__(self, api_host, page_id, api_key, allow_delete):
        self.sp_api_host = api_host + "/v1/pages/" + page_id
        self.allow_delete = allow_delete
        self.authorization_header = {'Authorization': 'OAuth ' + api_key}

    def sync_zbx_to_sp(self, zbx_info):
        """
        Get information from Statuspage about existing components. Match this information with zabbix
        service information. Create/Update/Delete the Statuspage components to match zabbix services
        :param zbx_info: List of ZabbixServiceInfo with information about zabbix services.
        """
        res_sp_components = requests.get(self.sp_api_host + "/components", headers=self.authorization_header, timeout=10).json()
        component_changes_made = False

        sp_info = []  # Hold list of Statuspage Component Information objects
        for spc in res_sp_components:  # For each Statuspage Component
            sp_info.append(StatusPageComponentInfo(spc["id"], spc["name"], spc["status"], spc["group"]))

        # Match Zabbix Services with Statuspage Components and Sync Differences
        for zbx_service in [c for c in zbx_info if not c.is_group_parent]:
            # Find the statuspage component with the same name as a zabbix service
            # FIXME : This implementation requires unique component names even between different groups
            sp_component = next((c for c in sp_info if c.component_name == zbx_service.service_name), None)

            if sp_component is not None:
                logging.debug("Matched a component on statuspage with a zabbix service. Named: {}. "
                              "Zabbix Service ID: {} Status Page ID: {}".
                              format(zbx_service.service_name, zbx_service.service_id, sp_component.component_id))

                # Make sure the status on the statuspage component is the same as the status on zabbix.
                sp_status = sp_component.component_status
                zbx_status = ZBX_SP_MAPPING[zbx_service.service_status]
                if sp_status != zbx_status:
                    logging.debug("Service: {} status mismatch (SP: {} ZBX: {}). Updating.".
                                  format(sp_component.component_name, sp_status, zbx_status))
                    self._update_component_status(sp_component.component_id, ZBX_SP_MAPPING[zbx_service.service_status])
                    sp_component.matched = True  # Exists on both zabbix & statuspage, don't delete it.
            else:
                self._create_component(zbx_service.service_name)
                component_changes_made = True

        # Delete components which were on statuspage but not on zabbix.
        if self.allow_delete:
            for sp_component in sp_info:
                if not sp_component.is_group and not sp_component.matched: # Ignore Groups
                    logging.debug("Found a component ({}) which exists on statuspage but not zabbix."
                                  "Configuration permits deletion".format(sp_component.component_id))
                    self._delete_component(sp_component.component_id)
                    component_changes_made = True

        # To modify component groups correctly, we need to most up-to-date information about the components on statuspage
        # Rather than recursively calling / sending another request, we can just wait until the next sync to update the groups
        if component_changes_made:
            logging.warn("Changes have been made to components during this sync. Updating component groups skipped and will be updated on the next sync")
            return  # Leave function

        sp_component_groups = requests.get(self.sp_api_host + "/component-groups", headers=self.authorization_header, timeout=10).json()

        # Component Group Sync
        for zbx_group in [g for g in zbx_info if g.is_group_parent]:
            # Find the statuspage component group with the same name as a zabbix group
            sp_group = next((spg for spg in sp_component_groups if zbx_group.service_name == spg["name"]), None)

            # Get the Statuspage component ID's of all the children of this group
            # FIXME : This implementation requires unique component names even between different groups
            group_children = list(filter(lambda gc: gc.linked_parent_id == zbx_group.service_id, zbx_info))
            children_name = [item.service_name for item in group_children]
            matched_components = list(filter(lambda item: item.component_name in children_name, sp_info))
            extracted_ids = [item.component_id for item in matched_components]

            if sp_group is not None:
                logging.debug("Found component group {} named {}. With children: {}".format(sp_group["id"], sp_group["name"], extracted_ids))
            else:
                logging.debug("Creating a new component group on statuspage: {} with components: {}".format(zbx_group.service_name, extracted_ids))
                self._create_component_group(zbx_group.service_name, extracted_ids)
                continue  # Newly created group, we don't need to continue to update the children again.

            # Check the ID's in the statuspage component group matches the children in the zabbix group.
            if len(set(extracted_ids) - set(sp_group["components"])) != 0:
                logging.debug("The children in the component group {} are not the same. Refreshing group children "
                              "to {}".format(zbx_group.id, extracted_ids))
                self._update_component_group(sp_group["id"], extracted_ids)

    def _create_component(self, name):
        url = self.sp_api_host + "/components/"
        if not DRY_RUN:
            res = requests.post(url, json={'component': {'name': name}}, headers=self.authorization_header, timeout=10)
            res.raise_for_status()
        logging.info("A new component has been created. Named: {}. The status will be updated during the next sync.".format(name))

    def _delete_component(self, component_id):
        url = self.sp_api_host + "/components/" + component_id
        if not DRY_RUN:
            res = requests.delete(url, headers=self.authorization_header, timeout=10)
            res.raise_for_status()
        logging.info("Deleted Component from Statuspage: {}".format(component_id))

    def _create_component_group(self, name, group_children):
        url = self.sp_api_host + "/component-groups"
        if not DRY_RUN:
            res = requests.post(url, json={'component_group': {'name': name, 'components': group_children}},
                                headers=self.authorization_header, timeout=10)
            res.raise_for_status()
        logging.info("A new component group has been created: {} which now contains {}.".format(name, group_children))

    def _update_component_group(self, component_group_id, group_children):
        url = self.sp_api_host + "/component-groups/" + component_group_id
        if not DRY_RUN:
            res = requests.put(url, json={'component_group': {'components': group_children}}, headers=self.authorization_header, timeout=10)
            res.raise_for_status()
        logging.info("Updated the component group: {} which now contains {}".format(component_group_id, group_children))

    def _update_component_status(self, component_id, status):
        url = self.sp_api_host + "/components/" + component_id
        logging.info("Setting component {} status to: {}".format(component_id, status))
        if not DRY_RUN:
            res = requests.patch(url, json={'component': {'status': status}}, headers=self.authorization_header, timeout=10)
            res.raise_for_status()
        logging.info("Updated the status of component: {} to {}.".format(component_id, status))


def send_webhook_alert(webhook_url, pageid, failed_attempts_count, exception):
    """
    Send a message to a webhook such as slack. Useful to announce statuspage sync failures
    :param webhook_url: URL of the webhook
    :param pageid: Identifier for the page
    :param failed_attempts_count: Count of how many consecutive failed attempts
    :param exception: Blank or the exception message causing the sync failures.
    """
    webhook_timeout = 60
    logging.debug("Post a message to {}".format(webhook_url))

    if failed_attempts_count > 0:
        msg = "Zabbix <-> Statuspage Sync Failure. The statuspage {} data may be out of date! \n" \
              "Amount of failed sync attempts: {}. {}".format(pageid, failed_attempts_count, exception)
    else:
        msg = "Zabbix <-> Statuspage sync Restored for page {}.".format(pageid)
    # Longer timeout incase of intermittent connectivity problems
    logging.info("Sending message to webhook. Will wait {} before continuing".format(webhook_timeout))
    res = requests.post(webhook_url, data=json.dumps({"text": msg}), headers={'Content-type': 'application/json'}, timeout=webhook_timeout)
    res.raise_for_status()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Synchronise Zabbix Services with Statuspage Components')
    parser.add_argument('-c', '--config', help="Path to script configuration file", type=str,default="zabbix_sync_to_statuspage_conf.yaml")
    parser.add_argument('-d', '--dryrun', help="Dry-run mode. The value won't actually be sent to Statuspage.", action='store_true')
    parser.add_argument('-l', '--logfile', help="Specify the log-file to store logging information.", default='zabbix_sync_to_statuspage.log')
    parser.add_argument('-s', '--screen', help="Print log details to screen (console)", action='store_true')
    parser.add_argument('-v', '--verbose', help="Verbose. Log debug information", action='store_true')

    args = parser.parse_args()
    DRY_RUN = args.dryrun

    log_handlers = []

    # Logging output
    if args.logfile != "":
        log_handlers.append(logging.FileHandler(args.logfile))
    if args.screen:
        log_handlers.append(logging.StreamHandler())

    # Logging verbosity
    logging_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(handlers=log_handlers, format=LOGGING_FORMAT, level=logging_level, datefmt=LOGGING_DATETIME_FORMAT)

    logging.info("------------------------------")
    logging.info("Sync Zabbix Services To Statuspage Components Starting")
    logging.info("------------------------------")

    try:
        with open(args.config, 'r') as stream:
            config = yaml.load(stream)

        zabbix_con = ZabbixService(config["zbx_api_host"], config["zbx_api_username"], config["zbx_api_password"])
        statuspage_con = StatusPageSync(config["sp_api_host"], config["sp_api_pageid"], config["sp_api_key"], config["sp_allow_dangling_component_delete"])
        delay = config["updateDelay"]
        alert_fail_attempts = int(config["alert_fail_attempts"])
        alert_msg_webhook = config["alert_msg_webhook"]
        bail_fail_attempts = int(config["bail_fail_attempts"])
        alert_include_exception = config["alert_include_exception"]

        failed_attempts_count = 0

        while True:  # Continue till exit or bail
            try:
                services_to_sync = zabbix_con.get_services(config["zabbix_root_service_id"])
                statuspage_con.sync_zbx_to_sp(services_to_sync)
                if failed_attempts_count > 0:
                    failed_attempts_count = 0  # Post to webhook saying its been restored.
                    send_webhook_alert(alert_msg_webhook, config["sp_api_pageid"], 0, "")

                logging.info("A Zabbix <-> Statuspage sync has completed. Waiting {}ms before the next sync.".format(str(delay)))
            except Exception as err:
                logging.error("Zabbix <-> Statuspage Sync failed. An exception occurred: {}".format(err))
                failed_attempts_count = failed_attempts_count + 1
                logging.info("Consecutive failed sync attempts: {}. Will retry in: {}ms".format(failed_attempts_count, delay))

                if failed_attempts_count == alert_fail_attempts and alert_msg_webhook != "":
                    exception = err if alert_include_exception else ""
                    send_webhook_alert(alert_msg_webhook, config["sp_api_pageid"], failed_attempts_count, exception)

                if (bail_fail_attempts != 0) and (failed_attempts_count >= bail_fail_attempts):
                    logging.fatal("Amount of consecutive sync attempts () greater than bail amount {}. Bailing-out.".
                                  format(failed_attempts_count, bail_fail_attempts))
                    exit(1)

            time.sleep(delay / 1000.0)
    except Exception as err:
        logging.error("An Unhandled Exception Occurred. Error: {}".format(traceback.print_exc()))
    finally:
        logging.info("*** Sync Zabbix Services to Statuspage Components Stopping ***")