"""This module includes classes representing RP API requests.

Detailed information about requests wrapped up in that module
can be found by the following link:
https://github.com/reportportal/documentation/blob/master/src/md/src/DevGuides/reporting.md
"""

#  Copyright (c) 2022 EPAM Systems
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#  https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License

import asyncio
import json as json_converter
import logging
import ssl
from typing import Callable, Text, Optional, Union, List, Tuple, Any, TypeVar

import aiohttp

from reportportal_client import helpers
from reportportal_client.core.rp_file import RPFile
from reportportal_client.core.rp_issues import Issue
from reportportal_client.core.rp_responses import RPResponse
from reportportal_client.helpers import dict_to_payload
from reportportal_client.static.abstract import (
    AbstractBaseClass,
    abstractmethod
)
from reportportal_client.static.defines import (
    DEFAULT_PRIORITY,
    LOW_PRIORITY,
    RP_LOG_LEVELS, Priority
)

logger = logging.getLogger(__name__)
T = TypeVar("T")


async def await_if_necessary(obj: Optional[Any]) -> Any:
    if obj:
        if asyncio.isfuture(obj) or asyncio.iscoroutine(obj):
            return await obj
        elif asyncio.iscoroutinefunction(obj):
            return await obj()
    return obj


class HttpRequest:
    """This model stores attributes related to RP HTTP requests."""

    session_method: Callable
    url: Any
    files: Optional[Any]
    data: Optional[Any]
    json: Optional[Any]
    verify_ssl: Optional[Union[bool, str]]
    http_timeout: Union[float, Tuple[float, float]]
    name: Optional[str]
    _priority: Priority
    _response: Optional[RPResponse]

    def __init__(self,
                 session_method: Callable,
                 url: Any,
                 data: Optional[Any] = None,
                 json: Optional[Any] = None,
                 files: Optional[Any] = None,
                 verify_ssl: Optional[bool] = None,
                 http_timeout: Union[float, Tuple[float, float]] = (10, 10),
                 name: Optional[Text] = None) -> None:
        """Initialize instance attributes.

        :param session_method: Method of the requests.Session instance
        :param url:            Request URL
        :param data:           Dictionary, list of tuples, bytes, or file-like
                               object to send in the body of the request
        :param json:           JSON to be sent in the body of the request
        :param verify_ssl:     Is SSL certificate verification required
        :param http_timeout:   a float in seconds for connect and read
                               timeout. Use a Tuple to specific connect and
                               read separately.
        :param name:           request name
        """
        self.data = data
        self.files = files
        self.json = json
        self.session_method = session_method
        self.url = url
        self.verify_ssl = verify_ssl
        self.http_timeout = http_timeout
        self.name = name
        self._priority = DEFAULT_PRIORITY
        self._response = None

    def __lt__(self, other) -> bool:
        """Priority protocol for the PriorityQueue."""
        return self.priority < other.priority

    @property
    def priority(self) -> Priority:
        """Get the priority of the request."""
        return self._priority

    @priority.setter
    def priority(self, value: Priority) -> None:
        """Set the priority of the request."""
        self._priority = value

    @property
    def response(self) -> Optional[RPResponse]:
        """Get the response object for the request."""
        if not self._response:
            return self.make()
        return self._response

    def make(self):
        """Make HTTP request to the Report Portal API."""
        try:
            self._response = RPResponse(
                self.session_method(self.url, data=self.data, json=self.json, files=self.files,
                                    verify=self.verify_ssl, timeout=self.http_timeout))
            return self._response
            # https://github.com/reportportal/client-Python/issues/39
        except (KeyError, IOError, ValueError, TypeError) as exc:
            logger.warning(
                "Report Portal %s request failed",
                self.name,
                exc_info=exc
            )


class AsyncHttpRequest(HttpRequest):
    """This model stores attributes related to RP HTTP requests."""

    def __init__(self, session_method: Callable, url: str, data=None, json=None,
                 files=None, verify_ssl=True, http_timeout=(10, 10),
                 name=None) -> None:
        super().__init__(session_method, url, data, json, files, verify_ssl, http_timeout, name)

    async def make(self):
        """Make HTTP request to the Report Portal API."""
        ssl_config = self.verify_ssl
        if ssl_config and type(ssl_config) == str:
            ssl_context = ssl.create_default_context()
            ssl_context.load_cert_chain(ssl_config)
            ssl_config = ssl_context

        timeout_config = self.http_timeout
        if not timeout_config or not type(timeout_config) == tuple:
            timeout_config = (timeout_config, timeout_config)

        data = self.data
        if self.files:
            data = self.files

        try:
            return RPResponse(
                await self.session_method(
                    await await_if_necessary(self.url),
                    data=data,
                    json=self.json,
                    ssl=ssl_config,
                    timeout=aiohttp.ClientTimeout(connect=timeout_config[0], sock_read=timeout_config[1])
                )
            )
            # https://github.com/reportportal/client-Python/issues/39
        except (KeyError, IOError, ValueError, TypeError) as exc:
            logger.warning(
                "Report Portal %s request failed",
                self.name,
                exc_info=exc
            )


class RPRequestBase(metaclass=AbstractBaseClass):
    """Base class for the rest of the RP request models."""

    __metaclass__ = AbstractBaseClass

    @abstractmethod
    def payload(self) -> dict:
        """Abstract interface for getting HTTP request payload."""
        raise NotImplementedError('Payload interface is not implemented!')


class LaunchStartRequest(RPRequestBase):
    """RP start launch request model.

    https://github.com/reportportal/documentation/blob/master/src/md/src/DevGuides/reporting.md#start-launch
    """
    attributes: Optional[Union[list, dict]]
    description: str
    mode: str
    name: str
    rerun: bool
    rerun_of: str
    start_time: str
    uuid: str

    def __init__(self,
                 name: str,
                 start_time: str,
                 attributes: Optional[Union[list, dict]] = None,
                 description: Optional[Text] = None,
                 mode: str = 'default',
                 rerun: bool = False,
                 rerun_of: Optional[Text] = None,
                 uuid: str = None) -> None:
        """Initialize instance attributes.

        :param name:        Name of the launch
        :param start_time:	Launch start time
        :param attributes:  Launch attributes
        :param description: Description of the launch
        :param mode:        Launch mode. Allowable values 'default' or 'debug'
        :param rerun:       Rerun mode. Allowable values 'True' of 'False'
        :param rerun_of:    Rerun mode. Specifies launch to be re-runned. Uses
                            with the 'rerun' attribute.
        """
        super().__init__()
        self.attributes = attributes
        self.description = description
        self.mode = mode
        self.name = name
        self.rerun = rerun
        self.rerun_of = rerun_of
        self.start_time = start_time
        self.uuid = uuid

    @property
    def payload(self) -> dict:
        """Get HTTP payload for the request."""
        if self.attributes and isinstance(self.attributes, dict):
            self.attributes = dict_to_payload(self.attributes)
        result = {
            'attributes': self.attributes,
            'description': self.description,
            'mode': self.mode,
            'name': self.name,
            'rerun': self.rerun,
            'rerunOf': self.rerun_of,
            'startTime': self.start_time
        }
        if self.uuid:
            result['uuid'] = self.uuid
        return result


class LaunchFinishRequest(RPRequestBase):
    """RP finish launch request model.

    https://github.com/reportportal/documentation/blob/master/src/md/src/DevGuides/reporting.md#finish-launch
    """

    def __init__(self,
                 end_time: str,
                 status: Optional[Text] = None,
                 attributes: Optional[Union[list, dict]] = None,
                 description: Optional[Text] = None) -> None:
        """Initialize instance attributes.

        :param end_time:    Launch end time
        :param status:      Launch status. Allowable values: "passed",
                            "failed", "stopped", "skipped", "interrupted",
                            "cancelled"
        :param attributes:  Launch attributes(tags). Pairs of key and value.
                            Overrides attributes on start
        :param description: Launch description. Overrides description on start
        """
        super().__init__()
        self.attributes = attributes
        self.description = description
        self.end_time = end_time
        self.status = status

    @property
    def payload(self) -> dict:
        """Get HTTP payload for the request."""
        if self.attributes and isinstance(self.attributes, dict):
            self.attributes = dict_to_payload(self.attributes)
        return {
            'attributes': self.attributes,
            'description': self.description,
            'endTime': self.end_time,
            'status': self.status
        }


class ItemStartRequest(RPRequestBase):
    """RP start test item request model.

    https://github.com/reportportal/documentation/blob/master/src/md/src/DevGuides/reporting.md#start-rootsuite-item
    """
    attributes: Optional[Union[list, dict]]
    code_ref: Optional[Text]
    description: Optional[Text]
    has_stats: bool
    launch_uuid: str
    name: str
    parameters: Optional[Union[list, dict]]
    retry: bool
    start_time: str
    test_case_id: Optional[Text]
    type_: str

    def __init__(self,
                 name: str,
                 start_time: str,
                 type_: str,
                 launch_uuid: str,
                 attributes: Optional[Union[list, dict]] = None,
                 code_ref: Optional[Text] = None,
                 description: Optional[Text] = None,
                 has_stats: bool = True,
                 parameters: Optional[Union[list, dict]] = None,
                 retry: bool = False,
                 test_case_id: Optional[Text] = None) -> None:
        """Initialize instance attributes.

        :param name:        Name of the test item
        :param start_time:  Test item start time
        :param type_:       Type of the test item. Allowable values: "suite",
                            "story", "test", "scenario", "step",
                            "before_class", "before_groups", "before_method",
                            "before_suite", "before_test", "after_class",
                            "after_groups", "after_method", "after_suite",
                            "after_test"
        :param launch_uuid: Parent launch UUID
        :param attributes:  Test item attributes
        :param code_ref:    Physical location of the test item
        :param description: Test item description
        :param has_stats:   Set to False if test item is nested step
        :param parameters:  Set of parameters (for parametrized test items)
        :param retry:       Used to report retry of the test. Allowable values:
                            "True" or "False"
        :param test_case_id:Test case ID from integrated TMS
        """
        super().__init__()
        self.attributes = attributes
        self.code_ref = code_ref
        self.description = description
        self.has_stats = has_stats
        self.launch_uuid = launch_uuid
        self.name = name
        self.parameters = parameters
        self.retry = retry
        self.start_time = start_time
        self.test_case_id = test_case_id
        self.type_ = type_

    @property
    def payload(self) -> dict:
        """Get HTTP payload for the request."""
        if self.attributes and isinstance(self.attributes, dict):
            self.attributes = dict_to_payload(self.attributes)
        if self.parameters:
            self.parameters = dict_to_payload(self.parameters)
        return {
            'attributes': self.attributes,
            'codeRef': self.code_ref,
            'description': self.description,
            'hasStats': self.has_stats,
            'launchUuid': self.launch_uuid,
            'name': self.name,
            'parameters': self.parameters,
            'retry': self.retry,
            'startTime': self.start_time,
            'testCaseId': self.test_case_id,
            'type': self.type_
        }


class ItemFinishRequest(RPRequestBase):
    """RP finish test item request model.

    https://github.com/reportportal/documentation/blob/master/src/md/src/DevGuides/reporting.md#finish-child-item
    """
    attributes: Optional[Union[list, dict]]
    description: str
    end_time: str
    is_skipped_an_issue: bool
    issue: Issue
    launch_uuid: str
    status: str
    retry: bool

    def __init__(self,
                 end_time: str,
                 launch_uuid: str,
                 status: str,
                 attributes: Optional[Union[list, dict]] = None,
                 description: Optional[str] = None,
                 is_skipped_an_issue: bool = True,
                 issue: Optional[Issue] = None,
                 retry: bool = False) -> None:
        """Initialize instance attributes.

        :param end_time:            Test item end time
        :param launch_uuid:         Parent launch UUID
        :param status:              Test status. Allowable values: "passed",
                                    "failed", "stopped", "skipped",
                                    "interrupted", "cancelled".
        :param attributes:          Test item attributes(tags). Pairs of key
                                    and value. Overrides attributes on start
        :param description:         Test item description. Overrides
                                    description from start request.
        :param is_skipped_an_issue: Option to mark skipped tests as not
                                    'To Investigate' items in UI
        :param issue:               Issue of the current test item
        :param retry:               Used to report retry of the test.
                                    Allowable values: "True" or "False"
        """
        super().__init__()
        self.attributes = attributes
        self.description = description
        self.end_time = end_time
        self.is_skipped_an_issue = is_skipped_an_issue
        self.issue = issue  # type: Issue
        self.launch_uuid = launch_uuid
        self.status = status
        self.retry = retry

    @property
    def payload(self) -> dict:
        """Get HTTP payload for the request."""
        if self.attributes and isinstance(self.attributes, dict):
            self.attributes = dict_to_payload(self.attributes)
        if self.issue is None and (
                self.status is not None and self.status.lower() == 'skipped'
        ) and not self.is_skipped_an_issue:
            issue_payload = {'issue_type': 'NOT_ISSUE'}
        else:
            issue_payload = None
        return {
            'attributes': self.attributes,
            'description': self.description,
            'endTime': self.end_time,
            'issue': getattr(self.issue, 'payload', issue_payload),
            'launchUuid': self.launch_uuid,
            'status': self.status,
            'retry': self.retry
        }


class RPRequestLog(RPRequestBase):
    """RP log save request model.

    https://github.com/reportportal/documentation/blob/master/src/md/src/DevGuides/reporting.md#save-single-log-without-attachment
    """
    file: Optional[RPFile]
    launch_uuid: str
    level: str
    message: Optional[Text]
    time: str
    item_uuid: Optional[Text]

    def __init__(self,
                 launch_uuid: str,
                 time: str,
                 file: Optional[RPFile] = None,
                 item_uuid: Optional[Text] = None,
                 level: str = RP_LOG_LEVELS[40000],
                 message: Optional[Text] = None) -> None:
        """Initialize instance attributes.

        :param launch_uuid: Launch UUID
        :param time:        Log time
        :param file:        Object of the RPFile
        :param item_uuid:   Test item UUID
        :param level:       Log level. Allowable values: error(40000),
                            warn(30000), info(20000), debug(10000),
                            trace(5000), fatal(50000), unknown(60000)
        :param message:     Log message
        """
        super().__init__()
        self.file = file  # type: RPFile
        self.launch_uuid = launch_uuid
        self.level = level
        self.message = message
        self.time = time
        self.item_uuid = item_uuid
        self.priority = LOW_PRIORITY

    def __file(self) -> dict:
        """Form file payload part of the payload."""
        if not self.file:
            return {}
        return {'file': {'name': self.file.name}}

    @property
    def payload(self) -> dict:
        """Get HTTP payload for the request."""
        payload = {
            'launchUuid': self.launch_uuid,
            'level': self.level,
            'message': self.message,
            'time': self.time,
            'itemUuid': self.item_uuid
        }
        payload.update(self.__file())
        return payload

    @property
    def multipart_size(self) -> int:
        """Calculate request size how it would transfer in Multipart HTTP."""
        size = helpers.calculate_json_part_size(self.payload)
        size += helpers.calculate_file_part_size(self.file)
        return size


class RPLogBatch(RPRequestBase):
    """RP log save batches with attachments request model.

    https://github.com/reportportal/documentation/blob/master/src/md/src/DevGuides/reporting.md#batch-save-logs
    """
    default_content: str = ...
    log_reqs: List[RPRequestLog] = ...

    def __init__(self, log_reqs: List[RPRequestLog]) -> None:
        """Initialize instance attributes.

        :param log_reqs:
        """
        super().__init__()
        self.default_content = 'application/octet-stream'
        self.log_reqs = log_reqs
        self.priority = LOW_PRIORITY

    def __get_file(self, rp_file) -> Tuple[str, tuple]:
        """Form a tuple for the single file."""
        return ('file', (rp_file.name,
                         rp_file.content,
                         rp_file.content_type or self.default_content))

    def __get_files(self) -> List[Tuple[str, tuple]]:
        """Get list of files for the JSON body."""
        files = []
        for req in self.log_reqs:
            if req.file:
                files.append(self.__get_file(req.file))
        return files

    def __get_request_part(self) -> List[Tuple[str, tuple]]:
        r"""Form JSON body for the request.

        Example:
        [('json_request_part',
          (None,
           '[{"launchUuid": "bf6edb74-b092-4b32-993a-29967904a5b4",
              "time": "1588936537081",
              "message": "Html report",
              "level": "INFO",
              "itemUuid": "d9dc2514-2c78-4c4f-9369-ee4bca4c78f8",
              "file": {"name": "Detailed report"}}]',
           'application/json')),
         ('file',
          ('Detailed report',
           '<html lang="utf-8">\n<body><p>Paragraph</p></body></html>',
           'text/html'))]
        """
        body = [(
            'json_request_part', (
                None,
                json_converter.dumps([log.payload for log in self.log_reqs]),
                'application/json'
            )
        )]
        body.extend(self.__get_files())
        return body

    @property
    def payload(self) -> List[Tuple[str, tuple]]:
        """Get HTTP payload for the request."""
        return self.__get_request_part()
