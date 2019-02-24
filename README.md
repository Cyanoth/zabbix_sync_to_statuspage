# Zabbix Services Synchronise to Statuspage Components

A python script which will periodically sychronise zabbix services to a Statuspage. This includes automatically creating or updating Statuspage components, component groups and statuses. Includes files to enable this script to run continuously in a docker container. 

This was mainly written for my own use, please feel free to fork and use it.

## Getting Started

You must first setup Zabbix services & Statuspage. Clone this repository to your local machine. 

On your Zabbix instance, click Configuration -> Services. Under the root service click 'Add a child'. After this has been created, select it on the service list. In the URL find 'serviceid=x', use this number in this script configuration for: `zabbix_root_service_id`. Any children services under this will be synchronised to Statuspage. Continue adding services under, selecting relevant tiggers. You can create a group, but not nested groups (Statuspage doesn't have nested group support).
See [zabbix services](https://www.zabbix.com/documentation/3.0/manual/web_interface/frontend_sections/configuration/itservices) for more information.

On Statuspage.io, create a new statuspage. In the URL, you can find the Page ID, this is required for the script's configuration value: `sp_api_pageid`. You may find the API key for `sp_api_key` in Manage Account. 

### Prerequisites

To run this script outside of a container. Ensure python3 is installed and run:
```
pip install -r requirements.txt
```

* This script has been tested on Zabbix v3.4
* To run this script as a container, ensure you have docker installed.

### Configuring

This script is configured by editing the file zabbix_sync_to_statuspage_conf.yaml. All values are required.

Example configuration:
```
zabbix_root_service_id: "2" # The ID of the top-most service. Entries under this will be synchronised.
updateDelay: 15000 # How often synchronisation should take place in ms. (Default: 15 seconds)

sp_api_host: "https://api.statuspage.io" # Hostname of Statuspage API.
sp_api_key: "fasfas-0123124-dfsdfa324-asdasdas-234124" # API key on Statuspage
sp_api_pageid: "sdf934k12ew" # Statuspage ID 
sp_allow_dangling_component_delete: False # Allows components to be deleted on Statuspage if they are not on Zabbix. DANGEROUS

zbx_api_host: "https://zabbix.mycoolhost.com" # Hostname of Zabbix API
zbx_api_username: "readonly" # Username that exists on the Zabbix instance. Must have read access to services & triggers.
zbx_api_password: "acdef01234" # Password for the user on the Zabbix instance.

alert_fail_attempts: 10 # Number of failed connection attempts before Posting a warning to a webhook
alert_msg_webhook: "https://hooks.mycoolhost.com/postmessage/asfasf324" # URL to post warning message when alert_fail_attempts exceeds 
alert_include_exception: True # Should the alert message include the exception that is causing failures
bail_fail_attempts: 0 # Number of failed connection attempts before this script bails. Put 0 to never bail from the script.

```


Now run the script:
```
python zabbix_sync_to_statuspage.py -v -s
```

To see the list of possible starting arguments, run:
```
python zabbix_sync_to_statuspage.py --help 
```

You should see the following:
```
INFO     ------------------------------
INFO     Sync Zabbix Services To Statuspage Components Starting
INFO     ------------------------------
DEBUG    Authenticating to Zabbix.
DEBUG    Starting new HTTPS connection (1): zabbix.mycoolhost.com:443
DEBUG    https://zabbix.mycoolhost.com:443 "POST //api_jsonrpc.php HTTP/1.1" 200 None
INFO     Authentication to Zabbix was successful. Session key obtained
DEBUG    Querying for Zabbix Services
DEBUG    Starting new HTTPS connection (1): zabbix.mycoolhost.com:443
DEBUG    https://zabbix.mycoolhost.com:443 "GET //api_jsonrpc.php HTTP/1.1" 200 None
DEBUG    Found a service with no children. ID: 3 Name: Website
DEBUG    Found a service with no children. ID: 8 Name: Code Search
DEBUG    Found a service with no children. ID: 4 Name: Git Operations
DEBUG    Found a service with no children. ID: 7 Name: Load Balancer
DEBUG    Found a service group with ID: 9 Name: Smart Mirrors
DEBUG    Found child service named Bangalore with id: 10 from parent named: Smart Mirrors with id: 9. It has no further descendants
DEBUG    Found child service named Miami with id: 12 from parent named: Smart Mirrors with id: 9. It has no further descendants
DEBUG    Found child service named Nanjing with id: 11 from parent named: Smart Mirrors with id: 9. It has no further descendants
```

## Deployment

After configuration, you can run this script continuously from within a docker container.

Within the working directory run:
```
docker build -t zabbix_sync_to_statuspage .
docker run zabbix_sync_to_statuspage
```
Note, that this will embedded the configuration file within the docker image.

Alternatively, You may use docker-compose to create a named volume and store the configuration there. This helps keep the docker image generic & configuration separate:
```
docker-compose build
docker-compose up -d
```
Customise the Dockerfile & docker-compose file to meet your deployment requirements.

## Known Issues
* All services must have a unique name, even if they are in different groups.