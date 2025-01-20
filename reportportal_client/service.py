import collections
import json
import requests
import uuid
from logging import getLogger
from pprint import pformat
from time import sleep

from .errors import ResponseError, EntryCreatedError, OperationCompletionError

logger = getLogger(__name__)


def _get_id(response):
    try:
        return _get_data(response)["id"]
    except KeyError:
        raise EntryCreatedError(
            "No 'id' in response: {0}".format(response.text))


def _get_name(response):
    try:
        return _get_data(response)["name"]
    except KeyError:
        raise EntryCreatedError(
            "No 'name' in response: {0}".format(response.text))


def _get_number(response):
    try:
        return _get_data(response)["number"]
    except KeyError:
        raise EntryCreatedError(
            "No 'id' in response: {0}".format(response.text))


def _get_msg(response):
    try:
        return _get_data(response)["msg"]
    except KeyError:
        raise OperationCompletionError(
            "No 'msg' in response: {0}".format(response.text))


def _get_data(response):
    data = _get_json(response)
    error_messages = _get_messages(data)
    error_count = len(error_messages)

    if error_count == 1:
        raise ResponseError(error_messages[0])
    elif error_count > 1:
        raise ResponseError(
            "\n  - ".join(["Multiple errors:"] + error_messages))
    elif not response.ok:
        response.raise_for_status()
    elif not data:
        raise ResponseError("Empty response")
    else:
        return data


def _get_json(response):
    try:
        if response.text:
            return response.json()
        else:
            return {}
    except ValueError as value_error:
        raise ResponseError(
            "Invalid response: {0}: {1}".format(value_error, response.text))


def _get_messages(data):
    error_messages = []
    for ret in data.get("responses", [data]):
        if "message" in ret:
            if "error_code" in ret:
                error_messages.append(
                    "{0}: {1}".format(ret["error_code"], ret["message"]))
            else:
                error_messages.append(ret["message"])

    return error_messages


def uri_join(*uri_parts):
    """Join uri parts.

    Avoiding usage of urlparse.urljoin and os.path.join
    as it does not clearly join parts.

    Args:
        *uri_parts: tuple of values for join, can contain back and forward
                    slashes (will be stripped up).

    Returns:
        An uri string.
    """
    return '/'.join(str(s).strip('/').strip('\\') for s in uri_parts)


class ReportPortalService(object):
    """Service class with report portal event callbacks."""

    def __init__(self, endpoint, project, token, api_base="api/v1"):
        """Init the service class.

        Args:
            endpoint: endpoint of report portal service.
            project: project name to use for launch names.
            token: authorization token.
            api_base: defaults to api/v1, can be changed to other version.
        """
        super(ReportPortalService, self).__init__()
        self.endpoint = endpoint
        self.api_base = api_base
        self.project = project
        self.token = token
        self.base_url = uri_join(self.endpoint,
                                 self.api_base,
                                 self.project)

        self.session = requests.Session()
        self.session.headers["Authorization"] = "bearer {0}".format(self.token)
        self.stack = [None]
        self.launch_id = None
        self.launch_name = None
        self.launch_number = None

    def terminate(self):
        pass

    def start_launch(self, name, start_time, description=None, tags=None,
                     mode=None):
        data = {
            "name": name,
            "description": description,
            "tags": tags,
            "start_time": start_time,
            "mode": mode
        }
        url = uri_join(self.base_url, "launch")
        logger.debug(f"Starting the RP Launch '{name}' by issuing the POST request as following:\n"
                     f"URL: {url}\ndata: {pformat(data)}")
        r = self.session.post(url=url, json=data)
        logger.debug(f"The response for start_launch is as following:\n{r.text}")
        self.launch_name = name
        self.launch_id = _get_id(r)
        self.launch_number = _get_number(r)
        self.stack.append(None)
        logger.debug("start_launch - Stack: %s", self.stack)
        return self.launch_id

    def update_existing_launch(self, launch_id):
        url = uri_join(self.base_url, "launch", launch_id)
        logger.debug(f"Updating the RP Launch '{launch_id}' by issuing the GET request as following:\n"
                     f"URL: {url}")
        r = self.session.get(url=url)
        logger.debug(f"The response for start_launch is as following:\n{r.text}")
        self.launch_name = _get_name(r)
        self.launch_id = launch_id
        self.launch_number = _get_number(r)
        self.stack.append(None)

    def _finalize_launch(self, end_time, action, status):
        data = {
            "end_time": end_time,
            "status": status
        }
        url = uri_join(self.base_url, "launch", self.launch_id, action)
        logger.debug(f"Performing the {action}_launch on launch {self.launch_id} by issuing the PUT request "
                     f"as following:\nURL: {url}\ndata:{pformat(data)}")
        r = self.session.put(url=url, json=data)
        logger.debug(f"The response for {action}_launch is as following:\n{r.text}")
        self.stack.pop()
        logger.debug("%s_launch - Stack: %s", action, self.stack)
        return _get_msg(r)

    def finish_launch(self, end_time, status=None):
        return self._finalize_launch(end_time=end_time, action="finish",
                                     status=status)

    def stop_launch(self, end_time, status=None):
        return self._finalize_launch(end_time=end_time, action="stop",
                                     status=status)

    def start_test_item(self, name, start_time, item_type, description=None,
                        tags=None, parameters=None):
        """
        item_type can be (SUITE, STORY, TEST, SCENARIO, STEP, BEFORE_CLASS,
        BEFORE_GROUPS, BEFORE_METHOD, BEFORE_SUITE, BEFORE_TEST, AFTER_CLASS,
        AFTER_GROUPS, AFTER_METHOD, AFTER_SUITE, AFTER_TEST)

        parameters should be a dictionary with the following format:
            {
                "<key1>": "<value1>",
                "<key2>": "<value2>",
                ...
            }
        """
        if parameters is not None:
            parameters = [{"key": key, "value": str(value)}
                          for key, value in parameters.items()]

        data = {
            "name": name,
            "description": description,
            "tags": tags,
            "start_time": start_time,
            "launch_id": self.launch_id,
            "type": item_type,
            "parameters": parameters,
        }
        parent_item_id = self.stack[-1]
        if parent_item_id is not None:
            url = uri_join(self.base_url, "item", parent_item_id)
        else:
            url = uri_join(self.base_url, "item")
        logger.debug(f"Starting the {item_type} test-item '{name}' by issuing the POST request as following:\n"
                     f"URL: {url}\ndata: {pformat(data)}")
        r = self.session.post(url=url, json=data)
        logger.debug(f"The response for start_test_item {item_type} test-item '{name}' is as "
                     f"following:\n{r.text}")
        item_id = _get_id(r)
        self.stack.append(item_id)
        logger.debug("start_test_item - Stack: %s", self.stack)
        return item_id

    def finish_test_item(self, end_time, status, issue=None):
        data = {
            "end_time": end_time,
            "status": status,
            "issue": issue,
        }
        item_id = self.stack.pop()
        url = uri_join(self.base_url, "item", item_id)
        logger.debug(f"Finishing the test-item {item_id} by issuing the PUT request as following:\n"
                     f"URL: {url}\ndata:{pformat(data)}")
        r = self.session.put(url=url, json=data)
        logger.debug(f"The response for finish_test_item {item_id} is as following:\n{r.text}")
        logger.debug("finish_test_item - Stack: %s", self.stack)
        return _get_msg(r)

    def log(self, time, message, level=None, attachment=None):
        data = {
            "item_id": self.stack[-1] or self.launch_id,
            "time": time,
            "message": message,
            "level": level,
        }
        if attachment:
            data["attachment"] = attachment
            return self.log_batch([data])
        else:
            url = uri_join(self.base_url, "log")
            logger.debug(f"Attempting to create a simple log entry by the POST request as following:\n"
                         f"URL: {url}\nitem_id: {data['item_id']}\nlevel: {data['level']}\ntime: {data['time']}")
            r = self.session.post(url=url, json=data)
            logger.debug(f"The response for the simple log entry creation is as following:\n{r.text}")
            logger.debug("log - Stack: %s", self.stack)
            return _get_id(r)

    def log_batch(self, log_data):
        """Logs batch of messages with attachment.

        Args:
            log_data: list of log records.
            log record is a dict of;
                time, message, level, attachment
                attachment is a dict of:
                    name: name of attachment
                    data: fileobj or content
                    mime: content type for attachment

        """

        url = uri_join(self.base_url, "log")
        item_id = self.stack[-1]
        attachments = []
        for log_item in log_data:
            log_item["item_id"] = item_id
            attachment = log_item.get("attachment", None)

            if "attachment" in log_item:
                del log_item["attachment"]

            if attachment:
                if not isinstance(attachment, collections.Mapping):
                    attachment = {"data": attachment}

                name = attachment.get("name", str(uuid.uuid4()))
                log_item["file"] = {"name": name}
                attachments.append(("file", (
                    name,
                    attachment["data"],
                    attachment.get("mime", "application/octet-stream")
                )))

        files = [(
            "json_request_part", (
                None,
                json.dumps(log_data),
                "application/json"
            )
        )]
        files.extend(attachments)
        info_msg = f"Attempting to create {len(log_data)} log-item entries with {len(attachments)} " \
                   f"attachments by the POST request as following:\nURL: {url}\n" \
                   f"log_items:\n{json.dumps(log_data)}\n"
        if attachments:
            info_msg += f"attachments:\n{attachments}"
        logger.debug(info_msg)
        attachment_retries = 10
        attachment_error = None
        for i in range(attachment_retries):
            logger.debug(f"Issuing HTTP-POST for the log-items creation - attempt number "
                         f"{i + 1}/{attachment_retries}")
            try:
                r = self.session.post(url=url, files=files)
                logger.debug("log_batch - Stack: %s", self.stack)
                logger.debug(logger.debug(f"The response for HTTP-POST of the log-items creation is as "
                                          f"following:\n{r.text}"))
                return _get_data(r)
            except requests.exceptions.RequestException as e:
                attachment_error = e
                logger.warning(f"The HTTP-POST for the log-items creation attempt {i + 1} has failed! {e}")
                logger.debug("Sleeping for 1 second..")
                sleep(1)
        logger.error(f"Failed to create the log-item entries since all {attachment_retries} attempts have "
                     f"failed due to error:\n{attachment_error}")
        logger.debug("Skipping the log-item entries creation")
