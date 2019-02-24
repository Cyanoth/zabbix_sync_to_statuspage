FROM python:3
ADD zabbix_sync_to_statuspage.py /
COPY requirements.txt requirements.txt
RUN pip install -r requirements.txt
COPY zabbix_sync_to_statuspage_conf.yaml /var/opt/zabbix_sync_to_statuspage/zabbix_sync_to_statuspage_conf.yaml
CMD [ "python", "./zabbix_sync_to_statuspage.py", "-c", "/var/opt/zabbix_sync_to_statuspage/zabbix_sync_to_statuspage_conf.yaml", "-v", "-s" ]