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

import threading
from logging import Logger
from queue import PriorityQueue
from threading import Thread
from typing import Any, Optional, Text, Union

from aenum import Enum

from reportportal_client.core.rp_requests import RPRequestBase as RPRequest, HttpRequest
from static.defines import Priority

logger: Logger
THREAD_TIMEOUT: int


class ControlCommand(Enum):
    CLEAR_QUEUE: Any = ...
    NOP: Any = ...
    REPORT_STATUS: Any = ...
    STOP: Any = ...
    STOP_IMMEDIATE: Any = ...

    def is_stop_cmd(self) -> bool: ...

    def __lt__(self, other: Union[ControlCommand, RPRequest]) -> bool: ...

    @property
    def priority(self) -> Priority: ...


class APIWorker:
    _queue: PriorityQueue = ...
    _thread: Optional[Thread] = ...
    _stop_lock: threading.Condition = ...
    name: Text = ...

    def __init__(self, task_queue: PriorityQueue) -> None: ...

    def _command_get(self) -> Optional[ControlCommand]: ...

    def _command_process(self, cmd: Optional[ControlCommand]) -> None: ...

    def _request_process(self, request: Optional[HttpRequest]) -> None: ...

    def _monitor(self) -> None: ...

    def _stop(self) -> None: ...

    def _stop_immediately(self) -> None: ...

    def is_alive(self) -> bool: ...

    def send(self, cmd: Union[ControlCommand, HttpRequest]) -> Any: ...

    def start(self) -> None: ...

    def __perform_stop(self, stop_command: ControlCommand) -> None: ...

    def stop(self) -> None: ...

    def stop_immediate(self) -> None: ...
