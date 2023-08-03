import demistomock as demisto  # noqa: F401
from CommonServerPython import *  # noqa: F401
import ast
import hashlib
import json
from collections import Counter


from datetime import datetime, timedelta, timezone


from laceworksdk import LaceworkClient
from laceworksdk.exceptions import ApiError

handle_proxy()

''' GLOBAL VARS '''
LACEWORK_ACCOUNT = demisto.params().get('lacework_account')
LACEWORK_SUBACCOUNT = demisto.params().get('lacework_subaccount', None)
LACEWORK_API_KEY = demisto.params()['lacework_api_key']
LACEWORK_API_SECRET = demisto.params()['lacework_api_secret']
LACEWORK_ALERT_SEVERITY = demisto.params()['lacework_event_severity']
LACEWORK_ALERT_HISTORY_DAYS = demisto.params()['lacework_event_history']

LACEWORK_DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
LACEWORK_ROW_LIMIT = 500000

try:
    if LACEWORK_SUBACCOUNT:
        lw_client = LaceworkClient(account=LACEWORK_ACCOUNT,
                                   subaccount=LACEWORK_SUBACCOUNT,
                                   api_key=LACEWORK_API_KEY,
                                   api_secret=LACEWORK_API_SECRET)
    else:
        lw_client = LaceworkClient(account=LACEWORK_ACCOUNT,
                                   api_key=LACEWORK_API_KEY,
                                   api_secret=LACEWORK_API_SECRET)
except Exception as e:
    demisto.results("Lacework API authentication failed. Please validate Account, \
                    Sub-Account, API Key, and API Secret. Error: {}".format(e))

''' HELPER FUNCTIONS '''


def get_alert_severity_int(sev_string):
    """
    Convert the Alert Severity string to the appropriate integer
    """

    sev_string = sev_string.lower()

    if sev_string == 'critical':
        return 1
    elif sev_string == 'high':
        return 2
    elif sev_string == 'medium':
        return 3
    elif sev_string == 'low':
        return 4
    elif sev_string in ('info', 'informational'):
        return 5
    else:
        raise Exception(f'Invalid Alert Severity Threshold was defined: {sev_string}')


def create_entry(title, data, ec, human_readable=None):
    """
    Simplify the output/contents
    """

    if human_readable is None:
        human_readable = data

    return {
        'ContentsFormat': formats['json'],
        'Type': entryTypes['note'],
        'Contents': data,
        'ReadableContentsFormat': formats['markdown'],
        'HumanReadable': tableToMarkdown(title, human_readable) if data else 'No result were found',
        'EntryContext': ec
    }


def create_search_json(start_time, end_time, filters, returns, time_delta=None):
    """
    Create a properly formatted JSON object with search parameters
    """

    json_request = {}

    now = datetime.now(timezone.utc)

    if time_delta is None:
        time_delta = timedelta(days=1)

    if start_time is None:
        start_time = now - time_delta
        start_time = start_time.strftime(LACEWORK_DATE_FORMAT)

    if end_time is None:
        end_time = now.strftime(LACEWORK_DATE_FORMAT)

    json_request['timeFilter'] = {
        'startTime': start_time,
        'endTime': end_time
    }

    if filters:
        json_request['filters'] = filters

    if returns:
        json_request['returns'] = returns

    return json_request


def create_vulnerability_ids(vulnerability_data):
    """
    Calculate Unique IDs for each vulnerability
    """

    for vulnerability in vulnerability_data:
        vulnerability_string = json.dumps(vulnerability).encode('utf-8')
        vulnerability['vulnHash'] = hashlib.new('md5', vulnerability_string, usedforsecurity=False).hexdigest()

    return vulnerability_data


def format_compliance_data(compliance_data, rec_id):
    """
    Simplify the output/contents for Compliance reports
    """

    if len(compliance_data['data']) > 0:

        compliance_data = compliance_data['data'][0]

        # If the user wants to filter on a recommendation ID
        if rec_id:
            rec_id = argToList(rec_id)
            # Iterate through all recommendations, removing irrelevant ones
            for recommendation in compliance_data["recommendations"][:]:
                if recommendation["REC_ID"] not in rec_id:
                    compliance_data["recommendations"].remove(recommendation)

        # Build Human Readable Output
        readable_output = tableToMarkdown("Compliance Summary",
                                          compliance_data['summary'])

        ec = {"Lacework.Compliance(val.reportTime === obj.reportTime)": compliance_data}
        return {
            'ContentsFormat': formats['json'],
            'Type': entryTypes['note'],
            'Contents': compliance_data,
            'ReadableContentsFormat': formats['markdown'],
            'HumanReadable': readable_output,
            'EntryContext': ec
        }
    else:
        return {
            "Type": entryTypes["error"],
            "ContentsFormat": formats["text"],
            "Contents": 'No compliance data was returned.'
        }


def execute_lql_query(start_time, end_time, query):
    """
    Execute supplied LQL query
    """

    arguments = {
        "StartTimeRange": start_time,
        "EndTimeRange": end_time 
    }

    try:
        response = lw_client.queries.execute(
            query_text=query,
            arguments=arguments
        )

        response_data = []
        for page in response.get('data'):
            response_data.append(page)
    except ApiError as e:
        raise Exception(
            'Error: {}'.format(e),
            'The Queries/execute parameters must follow the '
            'structure outlined in the Lacework API documentation: '
            'https://yourlacework.lacework.net/api/v2/docs/#tag/Queries/paths/~1api~1v2~1Queries~1execute/post'
        )
        
    return response_data


def cloudtrail_stats(cloudtrail, start_time, end_time, query, account_id, principal_id=None):
    """
    Pull statistics from returned cloud trail data
    """
    stats = {}
    stats["record_count"] = str(len(cloudtrail))
    user_agents = Counter([x['USERAGENT'] for x in cloudtrail if x['USERAGENT'] is not None]).most_common()
    for key, value in user_agents:
        stats["unique_user_agents"] = stats.get("unique_user_agents", "") + str(value) + " - " + key + "\n"
    events = Counter([x['EVENTNAME'] for x in cloudtrail if x['EVENTNAME'] is not None]).most_common()
    for key, value in events:
        stats["unique_events"] = stats.get("unique_events", "") + str(value) + " - " + key + "\n"
    stats["start_time"] = start_time
    stats["end_time"] = end_time
    stats["query"] = query.replace("\n", " ").replace("    ","").replace("  ", "")
    stats["account_id"] = account_id
    if principal_id:
        stats["principal_id"] = principal_id
    return stats


''' COMMANDS FUNCTIONS '''


def get_aws_compliance_assessment():
    """
    Get the latest AWS compliance assessment
    """

    account_id = demisto.args().get('account_id')
    rec_id = demisto.args().get('rec_id')
    report_type = demisto.args().get('report_type', 'AWS_CIS_S3')

    response = lw_client.reports.get(
        primary_query_id=account_id,
        format="json",
        type="COMPLIANCE",
        report_type=report_type,
        template_name="Default",
        latest=True
    )

    results = format_compliance_data(response, rec_id)
    return_results(results)


def get_azure_compliance_assessment():
    """
    Get the latest Azure compliance assessment
    """

    tenant_id = demisto.args().get('tenant_id')
    subscription_id = demisto.args().get('subscription_id')
    rec_id = demisto.args().get('rec_id')
    report_type = demisto.args().get('report_type', 'AZURE_CIS')

    response = lw_client.reports.get(
        primary_query_id=tenant_id,
        secondary_query_id=subscription_id,
        format="json",
        type="COMPLIANCE",
        report_type=report_type,
        template_name="Default",
        latest=True
    )

    results = format_compliance_data(response, rec_id)
    return_results(results)


def get_gcp_compliance_assessment():
    """
    Get the latest GCP compliance assessment
    """

    project_id = demisto.args().get('project_id')
    rec_id = demisto.args().get('rec_id')
    report_type = demisto.args().get('report_type', 'GCP_CIS')

    response = lw_client.reports.get(
        secondary_query_id=project_id,
        format="json",
        type="COMPLIANCE",
        report_type=report_type,
        template_name="Default",
        latest=True
    )

    results = format_compliance_data(response, rec_id)
    return_results(results)


def get_gcp_projects_by_organization():
    """
    Get a list of GCP Projects that reside in an Organization
    """

    organization_id = demisto.args().get('organization_id')

    response = lw_client.configs.gcp_projects.get(org_id=organization_id)

    ec = {"Lacework.GCP(val.organization === obj.organization)": response['data']}
    create_entry('Google Cloud Platform Projects for Organization ' + str(organization_id),
                 response['data'],
                 ec)


def get_container_vulnerabilities():
    """
    Get Container Vulnerabilities
    """

    start_time = demisto.args().get('start_time', None)
    end_time = demisto.args().get('end_time', None)
    filters = demisto.args().get('filters', None)
    returns = demisto.args().get('returns', None)
    limit = int(demisto.args().get('limit', LACEWORK_ROW_LIMIT))

    if filters:
        filters = ast.literal_eval(filters)
    if returns:
        returns = ast.literal_eval(returns)

    json_request = create_search_json(
        start_time=start_time,
        end_time=end_time,
        filters=filters,
        returns=returns
    )

    try:
        response = lw_client.vulnerabilities.containers.search(
            json=json_request
        )

        response_data = []
        current_rows = 0
        for page in response:
            take = limit - current_rows
            response_data += page['data'][:take]
            response_data = create_vulnerability_ids(response_data)
            current_rows = len(response_data)
            if current_rows >= limit:
                break
    except ApiError as e:
        raise Exception(
            'Error: {}'.format(e),
            'The vulnerability search parameters must follow the '
            'structure outlined in the Lacework API documentation: '
            'https://yourlacework.lacework.net/api/v2/docs#tag/Vulnerabilities'
        )

    ec = {"Lacework.Vulnerability.Container(val.vulnHash === obj.vulnHash)": response_data}
    return create_entry("Lacework Vulnerability Data for Containers",
                        response_data,
                        ec)


def get_host_vulnerabilities():
    """
    Get Host Vulnerabilities
    """

    start_time = demisto.args().get('start_time', None)
    end_time = demisto.args().get('end_time', None)
    filters = demisto.args().get('filters', None)
    returns = demisto.args().get('returns', None)
    limit = int(demisto.args().get('limit', LACEWORK_ROW_LIMIT))

    if filters:
        filters = ast.literal_eval(filters)
    if returns:
        returns = ast.literal_eval(returns)

    json_request = create_search_json(
        start_time=start_time,
        end_time=end_time,
        filters=filters,
        returns=returns
    )

    try:
        response = lw_client.vulnerabilities.hosts.search(
            json=json_request
        )

        response_data = []
        current_rows = 0
        for page in response:
            take = limit - current_rows
            response_data += page['data'][:take]
            response_data = create_vulnerability_ids(response_data)
            current_rows = len(response_data)
            if current_rows >= limit:
                break
    except ApiError as e:
        raise Exception(
            'Error: {}'.format(e),
            'The vulnerability search parameters must follow the '
            'structure outlined in the Lacework API documentation: '
            'https://yourlacework.lacework.net/api/v2/docs#tag/Vulnerabilities'
        )

    ec = {"Lacework.Vulnerability.Host(val.vulnHash === obj.vulnHash)": response_data}
    return create_entry("Lacework Vulnerability Data for Hosts",
                        response_data,
                        ec)


def get_alert_details():
    """
    Get Alert Details
    """

    alert_id = demisto.args().get('alert_id')
    scope = demisto.args().get('scope', 'Details')

    response = lw_client.alerts.get_details(alert_id, scope)

    ec = {"Lacework.Alert(val.alertId === obj.alertId)": response['data']}
    return create_entry("Lacework Alert " + str(alert_id),
                        response['data'],
                        ec)


def get_compliance_report():
    """
    Get Compliance Report
    """

    primary_query_id = demisto.args().get('primary_query_id')
    secondary_query_id = demisto.args().get('secondary_query_id')
    report_name = demisto.args().get('report_name')
    report_type = demisto.args().get('report_type')
    template_name = demisto.args().get('template_name')

    # Optional filtering
    rec_id = demisto.args().get('rec_id')

    response = lw_client.reports.get(
        primary_query_id=primary_query_id,
        secondary_query_id=secondary_query_id,
        format="json",
        type="COMPLIANCE",
        report_name=report_name,
        report_type=report_type,
        template_name=template_name,
        latest=True
    )

    results = format_compliance_data(response, rec_id)
    return_results(results)


def fetch_incidents():
    """
    Function to fetch incidents (alerts) from Lacework
    """

    # Make a placeholder for events
    new_incidents = []

    # Get data from the last run
    max_alert_id = demisto.getLastRun().get('max_alert_id', 0)

    now = datetime.now(timezone.utc)

    # Generate ISO8601 Timestamps
    end_time = now.strftime(LACEWORK_DATE_FORMAT)
    start_time = now - timedelta(days=int(LACEWORK_ALERT_HISTORY_DAYS))
    start_time = start_time.strftime(LACEWORK_DATE_FORMAT)

    # Get the alert severity threshold
    alert_severity_threshold = get_alert_severity_int(LACEWORK_ALERT_SEVERITY)

    # Get alerts from Lacework
    alerts_response = lw_client.alerts.get(start_time, end_time)
    alerts_data = alerts_response.get('data', [])

    temp_max_alert_id = max_alert_id

    # Iterate through all alerts
    for alert in alerts_data:

        # Convert the current Alert ID to an integer
        alert_id = int(alert['alertId'])
        # Get the numeric value for severity
        alert_severity = get_alert_severity_int(alert['severity'])

        # If the alert is severe enough, continue
        if alert_severity <= alert_severity_threshold:

            # If the Alert ID is newer than we've imported, then add it
            if alert_id > max_alert_id:

                # Store our new max Alert ID
                if alert_id > temp_max_alert_id:
                    temp_max_alert_id = alert_id

                # Get the event details from Lacework
                alert_details = lw_client.alerts.get_details(alert['alertId'], 'Details')

                incident = {
                    'name': 'Lacework Event: ' + alert['alertType'],
                    'occurred': alert['startTime'],
                    'rawJSON': json.dumps(alert_details['data'])
                }

                new_incidents.append(incident)

    max_alert_id = temp_max_alert_id

    demisto.setLastRun({
        'max_alert_id': max_alert_id
    })
    demisto.incidents(new_incidents)


def get_activities_changedfiles():
    """
    Search activities / changed files endpoint
    """

    start_time = demisto.args().get('start_time', None)
    end_time = demisto.args().get('end_time', None)
    filters = demisto.args().get('filters', [])
    returns = demisto.args().get('returns', None)
    limit = int(demisto.args().get('limit', LACEWORK_ROW_LIMIT))
    mid = demisto.args().get('mid', None)

    if filters:
        filters = ast.literal_eval(filters)
    if returns:
        returns = ast.literal_eval(returns)
    if mid:
        filters.append({ "field" : "mid", "expression": "eq", "value": mid })
    if len(filters) < 1:
        filters = None

    json_request = create_search_json(
        start_time=start_time,
        end_time=end_time,
        filters=filters,
        returns=returns
    )

    try:
        response = lw_client.activities.changed_files.search(
            json=json_request
        )

        response_data = []
        current_rows = 0
        for page in response:
            take = limit - current_rows
            response_data += page['data'][:take]
            current_rows = len(response_data)
            if current_rows >= limit:
                break
    except ApiError as e:
        raise Exception(
            'Error: {}'.format(e),
            'The Activities/ChangedFiles search parameters must follow the '
            'structure outlined in the Lacework API documentation: '
            'https://yourlacework.lacework.net/api/v2/docs/#tag/Activities/paths/~1api~1v2~1Activities~1ChangedFiles~1search/post'
        )

    # ec = {"Lacework.Activities.ChangedFiles(val.vulnHash === obj.vulnHash)": response_data}
    ec = {"Lacework.Activities.ChangedFiles()": response_data}
    return create_entry("Lacework changed files from activities data",
                        response_data,
                        ec)


def get_activities_connections():
    """
    Search activities / connections endpoint
    """

    start_time = demisto.args().get('start_time', None)
    end_time = demisto.args().get('end_time', None)
    filters = demisto.args().get('filters', [])
    returns = demisto.args().get('returns', None)
    limit = int(demisto.args().get('limit', LACEWORK_ROW_LIMIT))
    src_mid = demisto.args().get('src_mid', None)
    dst_mid = demisto.args().get('dst_mid', None)

    if filters:
        filters = ast.literal_eval(filters)
    if returns:
        returns = ast.literal_eval(returns)
    if src_mid:
        filters.append({ "field" : "srcEntityId.mid", "expression": "eq", "value": src_mid })
    if dst_mid:
        filters.append({ "field" : "dstEntityId.mid", "expression": "eq", "value": dst_mid })
    if len(filters) < 1:
        filters = None

    json_request = create_search_json(
        start_time=start_time,
        end_time=end_time,
        filters=filters,
        returns=returns
    )

    try:
        response = lw_client.activities.connections.search(
            json=json_request
        )

        response_data = []
        current_rows = 0
        for page in response:
            take = limit - current_rows
            response_data += page['data'][:take]
            current_rows = len(response_data)
            if current_rows >= limit:
                break
    except ApiError as e:
        raise Exception(
            'Error: {}'.format(e),
            'The Activities/Connections search parameters must follow the '
            'structure outlined in the Lacework API documentation: '
            'https://yourlacework.lacework.net/api/v2/docs/#tag/Activities/paths/~1api~1v2~1Activities~1Connections~1search/post'
        )

    # ec = {"Lacework.Activities.Connections(val.vulnHash === obj.vulnHash)": response_data}
    ec = {"Lacework.Activities.Connections()": response_data}
    return create_entry("Lacework connections from activities data",
                        response_data,
                        ec)



    return create_entry("Connections by machine id: " + mid,
                        response,
                        None)


def get_activities_dns():
    """
    Search DNS summaries by mid
    """

    start_time = demisto.args().get('start_time', None)
    end_time = demisto.args().get('end_time', None)
    filters = demisto.args().get('filters', [])
    returns = demisto.args().get('returns', None)
    limit = int(demisto.args().get('limit', LACEWORK_ROW_LIMIT))
    mid = demisto.args().get('mid', None)

    if filters:
        filters = ast.literal_eval(filters)
    if returns:
        returns = ast.literal_eval(returns)
    if mid:
        filters.append({ "field" : "mid", "expression": "eq", "value": mid })
    if len(filters) < 1:
        filters = None

    json_request = create_search_json(
        start_time=start_time,
        end_time=end_time,
        filters=filters,
        returns=returns
    )

    try:
        response = lw_client.activities.dns.search(
            json=json_request
        )

        response_data = []
        current_rows = 0
        for page in response:
            take = limit - current_rows
            response_data += page['data'][:take]
            current_rows = len(response_data)
            if current_rows >= limit:
                break
    except ApiError as e:
        raise Exception(
            'Error: {}'.format(e),
            'The Activities/DNSs search parameters must follow the '
            'structure outlined in the Lacework API documentation: '
            'https://yourlacework.lacework.net/api/v2/docs/#tag/Activities/paths/~1api~1v2~1Activities~1DNSs~1search/post'
        )

    # ec = {"Lacework.Activities.DNSs(val.vulnHash === obj.vulnHash)": response_data}
    ec = {"Lacework.Activities.DNSs()": response_data}
    return create_entry("Lacework DNS data",
                        response_data,
                        ec)


def get_activities_userlogins():
    """
    Search user logins by machine id or username
    """

    start_time = demisto.args().get('start_time', None)
    end_time = demisto.args().get('end_time', None)
    filters = demisto.args().get('filters', [])
    returns = demisto.args().get('returns', None)
    limit = int(demisto.args().get('limit', LACEWORK_ROW_LIMIT))
    mid = demisto.args().get('mid', None)
    username = demisto.args().get('username', None)

    if filters:
        filters = ast.literal_eval(filters)
    if returns:
        returns = ast.literal_eval(returns)
    if mid:
        filters.append({ "field" : "mid", "expression": "eq", "value": mid })
    if username:
        filters.append({ "field" : "username", "expression": "eq", "value": username })
    if len(filters) < 1:
        filters = None

    json_request = create_search_json(
        start_time=start_time,
        end_time=end_time,
        filters=filters,
        returns=returns
    )

    try:
        response = lw_client.activities.user_logins.search(
            json=json_request
        )

        response_data = []
        current_rows = 0
        for page in response:
            take = limit - current_rows
            response_data += page['data'][:take]
            current_rows = len(response_data)
            if current_rows >= limit:
                break
    except ApiError as e:
        raise Exception(
            'Error: {}'.format(e),
            'The Activities/UserLogins search parameters must follow the '
            'structure outlined in the Lacework API documentation: '
            'https://yourlacework.lacework.net/api/v2/docs/#tag/Activities/paths/~1api~1v2~1Activities~1UserLogins~1search/post'
        )

    # ec = {"Lacework.Activities.DNSs(val.vulnHash === obj.vulnHash)": response_data}
    ec = {"Lacework.Activities.UserLogins()": response_data}
    return create_entry("Lacework user login data",
                        response_data,
                        ec)


def get_entities_machines():
    """
    Search machines by hostname, ip, or machine id
    """

    start_time = demisto.args().get('start_time', None)
    end_time = demisto.args().get('end_time', None)
    filters = demisto.args().get('filters', [])
    returns = demisto.args().get('returns', None)
    limit = int(demisto.args().get('limit', LACEWORK_ROW_LIMIT))
    mid = demisto.args().get('mid', None)
    hostname = demisto.args().get('hostname', None)
    internal_ip = demisto.args().get('internal_ip', None)
    external_ip = demisto.args().get('external_ip', None)

    if filters:
        filters = ast.literal_eval(filters)
    if returns:
        returns = ast.literal_eval(returns)
    if mid:
        filters.append({ "field" : "mid", "expression": "eq", "value": mid })
    if hostname:
        filters.append({ "field" : "hostname", "expression": "eq", "value": hostname })
    if internal_ip:
        filters.append({ "field" : "machineTags.InternalIp", "expression": "eq", "value": internal_ip })
    if external_ip:
        filters.append({ "field" : "machineTags.ExternalIp", "expression": "eq", "value": external_ip })
    if len(filters) < 1:
        filters = None

    json_request = create_search_json(
        start_time=start_time,
        end_time=end_time,
        filters=filters,
        returns=returns
    )

    try:
        response = lw_client.entities.machines.search(
            json=json_request
        )

        response_data = []
        current_rows = 0
        for page in response:
            take = limit - current_rows
            response_data += page['data'][:take]
            current_rows = len(response_data)
            if current_rows >= limit:
                break
    except ApiError as e:
        raise Exception(
            'Error: {}'.format(e),
            'The Entities/Machines search parameters must follow the '
            'structure outlined in the Lacework API documentation: '
            'https://yourlacework.lacework.net/api/v2/docs/#tag/Entities/paths/~1api~1v2~1Entities~1Machines~1search/post'
        )

    # ec = {"Lacework.Activities.DNSs(val.vulnHash === obj.vulnHash)": response_data}
    ec = {"Lacework.Entities.Machines()": response_data}
    return create_entry("Lacework machine data",
                        response_data,
                        ec)


def get_entities_machinedetails():
    """
    Search machine details by mid
    """

    start_time = demisto.args().get('start_time', None)
    end_time = demisto.args().get('end_time', None)
    filters = demisto.args().get('filters', [])
    returns = demisto.args().get('returns', None)
    limit = int(demisto.args().get('limit', LACEWORK_ROW_LIMIT))
    mid = demisto.args().get('mid', None)

    if filters:
        filters = ast.literal_eval(filters)
    if returns:
        returns = ast.literal_eval(returns)
    if mid:
        filters.append({ "field" : "mid", "expression": "eq", "value": mid })
    if len(filters) < 1:
        filters = None

    json_request = create_search_json(
        start_time=start_time,
        end_time=end_time,
        filters=filters,
        returns=returns
    )

    try:
        response = lw_client.entities.machine_details.search(
            json=json_request
        )

        response_data = []
        current_rows = 0
        for page in response:
            take = limit - current_rows
            response_data += page['data'][:take]
            current_rows = len(response_data)
            if current_rows >= limit:
                break
    except ApiError as e:
        raise Exception(
            'Error: {}'.format(e),
            'The Entities/MachineDetails search parameters must follow the '
            'structure outlined in the Lacework API documentation: '
            'https://yourlacework.lacework.net/api/v2/docs/#tag/Entities/paths/~1api~1v2~1Entities~1MachineDetails~1search/post'
        )

    # ec = {"Lacework.Activities.DNSs(val.vulnHash === obj.vulnHash)": response_data}
    ec = {"Lacework.Entities.MachineDetails()": response_data}
    return create_entry("Lacework machine details data",
                        response_data,
                        ec)


def get_entities_processes():
    """
    Search active processes by mid
    """

    start_time = demisto.args().get('start_time', None)
    end_time = demisto.args().get('end_time', None)
    filters = demisto.args().get('filters', [])
    returns = demisto.args().get('returns', None)
    limit = int(demisto.args().get('limit', LACEWORK_ROW_LIMIT))
    mid = demisto.args().get('mid', None)
    pid = demisto.args().get('pid', None)
    ppid = demisto.args().get('ppid', None)

    if filters:
        filters = ast.literal_eval(filters)
    if returns:
        returns = ast.literal_eval(returns)
    if mid:
        filters.append({ "field" : "mid", "expression": "eq", "value": mid })
    if pid:
        filters.append({ "field" : "pid", "expression": "eq", "value": pid })
    if ppid:
        filters.append({ "field" : "ppid", "expression": "eq", "value": ppid })
    if len(filters) < 1:
        filters = None

    json_request = create_search_json(
        start_time=start_time,
        end_time=end_time,
        filters=filters,
        returns=returns
    )

    try:
        response = lw_client.entities.processes.search(
            json=json_request
        )

        response_data = []
        current_rows = 0
        for page in response:
            take = limit - current_rows
            response_data += page['data'][:take]
            current_rows = len(response_data)
            if current_rows >= limit:
                break
    except ApiError as e:
        raise Exception(
            'Error: {}'.format(e),
            'The Entities/Processes search parameters must follow the '
            'structure outlined in the Lacework API documentation: '
            'https://yourlacework.lacework.net/api/v2/docs/#tag/Entities/paths/~1api~1v2~1Entities~1Processes~1search/post'
        )

    # ec = {"Lacework.Activities.DNSs(val.vulnHash === obj.vulnHash)": response_data}
    ec = {"Lacework.Entities.Processes()": response_data}
    return create_entry("Lacework process data",
                        response_data,
                        ec)


def get_entities_commandlines():
    """
    Search active command lines by cmdLineHasH
    """

    start_time = demisto.args().get('start_time', None)
    end_time = demisto.args().get('end_time', None)
    filters = demisto.args().get('filters', [])
    returns = demisto.args().get('returns', None)
    limit = int(demisto.args().get('limit', LACEWORK_ROW_LIMIT))
    cmdline_hash = demisto.args().get('cmdlineHash', None)

    if filters:
        filters = ast.literal_eval(filters)
    if returns:
        returns = ast.literal_eval(returns)
    if cmdline_hash:
        filters.append({ "field" : "cmdlineHash", "expression": "eq", "value": cmdline_hash })
    if len(filters) < 1:
        filters = None

    json_request = create_search_json(
        start_time=start_time,
        end_time=end_time,
        filters=filters,
        returns=returns
    )

    try:
        response = lw_client.entities.command_lines.search(
            json=json_request
        )
        response_data = []
        current_rows = 0
        for page in response:
            take = limit - current_rows
            response_data += page['data'][:take]
            current_rows = len(response_data)
            if current_rows >= limit:
                break
    except ApiError as e:
        raise Exception(
            'Error: {}'.format(e),
            'The Entities/CommandLines search parameters must follow the '
            'structure outlined in the Lacework API documentation: '
            'https://yourlacework.lacework.net/api/v2/docs/#tag/Entities/paths/~1api~1v2~1Entities~1CommandLines~1search/post'
        )

    # ec = {"Lacework.Activities.DNSs(val.vulnHash === obj.vulnHash)": response_data}
    ec = {"Lacework.Entities.CommandLines()": response_data}
    return create_entry("Lacework command line data",
                        response_data,
                        ec)


def get_queries_validate():
    """
    Validate supplied LQL query
    """

    query = demisto.args().get('query')

    try:
        response = lw_client.queries.validate(
            query_text=query
        )

        response_data = {"queryId": response['data']['queryId'],
                         "query": response['data']['queryText']}

    except ApiError as e:
        raise Exception(
            'Error: {}'.format(e),
            'The Queries/validate search parameters must follow the '
            'structure outlined in the Lacework API documentation: '
            'https://yourlacework.lacework.net/api/v2/docs/#tag/Queries/paths/~1api~1v2~1Queries~1validate/post'
        )
    ec = {"Lacework.Queries.validate()": response_data}
    return create_entry("Lacework query validation",
                        response_data,
                        ec)
    
    
def get_queries_execute():
    """
    Execute supplied LQL query
    """

    start_time = demisto.args().get('start_time', None)
    end_time = demisto.args().get('end_time', None)
    query = demisto.args().get('query')

    response_data = execute_lql_query(start_time, end_time, query)

    ec = {"Lacework.Queries.execute()": response_data}
    return create_entry("Lacework LQL query data",
                        response_data,
                        ec)


def get_inventory_search():
    """
    Search cloud inventory
    """

    start_time = demisto.args().get('start_time', None)
    end_time = demisto.args().get('end_time', None)
    filters = demisto.args().get('filters', [])
    returns = demisto.args().get('returns', None)
    limit = int(demisto.args().get('limit', LACEWORK_ROW_LIMIT))
    csp = demisto.args().get('csp')
    resource_id = demisto.args().get('resource_id', None)
    account_id = demisto.args().get('account_id', None)


    if filters:
        filters = ast.literal_eval(filters)
    if returns:
        returns = ast.literal_eval(returns)
    if resource_id:
        filters.append({ "field" : "resource_id", "expression": "eq", "value": resource_id})
    if resource_id:
        filters.append({ "field" : "cloudDetails.accountID", "expression": "eq", "value": account_id })
    if len(filters) < 1:
        filters = None

    json_request = create_search_json(
        start_time=start_time,
        end_time=end_time,
        filters=filters,
        returns=returns
    )

    json_request['csp'] = csp
    
    try:
        response = lw_client.inventory.search(
            json=json_request
        )

        response_data = []
        current_rows = 0
        for page in response:
            take = limit - current_rows
            response_data += page['data'][:take]
            current_rows = len(response_data)
            if current_rows >= limit:
                break
    except ApiError as e:
        raise Exception(
            'Error: {}'.format(e),
            'The Inventory/search parameters must follow the '
            'structure outlined in the Lacework API documentation: '
            'https://yourlacework.lacework.net/api/v2/docs/#tag/Inventory/paths/~1api~1v2~1Inventory~1search/post'
        )

    ec = {"Lacework.Inventory.search()": response_data}
    return create_entry("Lacework cloud inventory search data",
                        response_data,
                        ec)


def get_cloudtrail_search():
    """
    Use get_queries_execute function to search cloudtrail using template in this function.
    """
    start_time = demisto.args().get('start_time', None)
    end_time = demisto.args().get('end_time', None)
    account_id = demisto.args().get('account_id')
    principal_id = demisto.args().get('principal_id', '')
    output = demisto.args().get('output', 'stats')
    
    
    filter = f"E.EVENT:recipientAccountId = '{account_id}' "
    if principal_id:
        filter += f"and E.EVENT:userIdentity.principalId = '{principal_id}' "
    
    query =   """
    {
      source { CloudTrailRawEvents E }
      filter {
        [[filter]]
      }
      return distinct {
        EVENT:eventName::String as eventName, EVENT:eventTime::String as eventTime, 
        EVENT:recipientAccountAlias::String as accountAlias, EVENT:eventID::String as eventID,
        EVENT:recipientAccountId::String as accountId, EVENT:sourceIPAddress::String as sourceIPAddress,
        EVENT:userIdentity.userName::String as userName, EVENT:userAgent::String as userAgent
      }
    }""".replace("[[filter]]", filter)
        
    response_data = execute_lql_query(start_time, end_time, query)
    
    if output == "file":
        return fileResult('cloudtrail.json', json.dumps(response_data))

    human_readable = cloudtrail_stats(response_data, start_time, end_time, query, account_id, principal_id)
    ec = {"Lacework.CloudTrail.search()": human_readable}
    return create_entry("Lacework cloud inventory search data",
                        human_readable,
                        ec,
                        human_readable)


''' EXECUTION CODE '''


try:
    command = demisto.command()
    demisto.debug(f'Command being called is {command}')
    if demisto.command() == 'test-module':
        # This is the call made when pressing the integration test button.
        try:
            demisto.debug('Getting User Profile for "test-module" run')
            response = lw_client.user_profile.get()
            demisto.debug(response)

            keys = set(['username', 'url', 'accounts'])
            if keys.issubset(response['data'][0].keys()):
                demisto.results('ok')
        except Exception as error:
            demisto.results(error)
    elif demisto.command() == 'lw-get-aws-compliance-assessment':
        demisto.results(get_aws_compliance_assessment())
    elif demisto.command() == 'lw-get-azure-compliance-assessment':
        demisto.results(get_azure_compliance_assessment())
    elif demisto.command() == 'lw-get-gcp-compliance-assessment':
        demisto.results(get_gcp_compliance_assessment())
    elif demisto.command() == 'lw-get-gcp-projects-by-organization':
        demisto.results(get_gcp_projects_by_organization())
    elif demisto.command() == 'lw-get-container-vulnerabilities':
        demisto.results(get_container_vulnerabilities())
    elif demisto.command() == 'lw-get-host-vulnerabilities':
        demisto.results(get_host_vulnerabilities())
    elif demisto.command() == 'lw-get-compliance-report':
        demisto.results(get_compliance_report())
    elif demisto.command() == 'lw-get-alert-details':
        demisto.results(get_alert_details())
    elif demisto.command() == 'fetch-incidents':
        demisto.results(fetch_incidents())
    elif demisto.command() == 'lw-get-activities-changedfiles':
        demisto.results(get_activities_changedfiles())
    elif demisto.command() == 'lw-get-activities-connections':
        demisto.results(get_activities_connections())
    elif demisto.command() == 'lw-get-activities-dns':
        demisto.results(get_activities_dns())
    elif demisto.command() == 'lw-get-activities-userlogins':
        demisto.results(get_activities_userlogins())
    elif demisto.command() == 'lw-get-entities-machines':
        demisto.results(get_entities_machines())
    elif demisto.command() == 'lw-get-entities-machinedetails':
        demisto.results(get_entities_machinedetails())
    elif demisto.command() == 'lw-get-entities-processes':
        demisto.results(get_entities_processes())
    elif demisto.command() == 'lw-get-entities-commandlines':
        demisto.results(get_entities_commandlines())
    elif demisto.command() == 'lw-get-queries-validate':
        demisto.results(get_queries_validate())
    elif demisto.command() == 'lw-get-queries-execute':
        demisto.results(get_queries_execute())
    elif demisto.command() == 'lw-get-inventory-search':
        demisto.results(get_inventory_search())
    elif demisto.command() == 'lw-get-cloudtrail-search':
        return_results(get_cloudtrail_search())
except Exception as e:
    LOG(e)
    LOG.print_log()
    raise
