# Copyright 2015 Cloudbase Solutions Srl
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import eventlet

from nova.i18n import _LW
from nova.virt import event as virtevent
from os_win import constants
from os_win import exceptions as os_win_exc
from os_win import utilsfactory
from oslo_config import cfg
from oslo_log import log as logging

from hyperv.nova import serialconsoleops

LOG = logging.getLogger(__name__)

hyperv_opts = [
    cfg.IntOpt('power_state_check_timeframe',
                default=60,
                help='The timeframe to be checked for instance power '
                     'state changes.'),
    cfg.IntOpt('power_state_event_polling_interval',
                default=2,
                help='Instance power state change event polling frequency.'),

]

CONF = cfg.CONF
CONF.register_opts(hyperv_opts, 'hyperv')


class InstanceEventHandler(object):
    _TRANSITION_MAP = {
        constants.HYPERV_VM_STATE_ENABLED: virtevent.EVENT_LIFECYCLE_STARTED,
        constants.HYPERV_VM_STATE_DISABLED: virtevent.EVENT_LIFECYCLE_STOPPED,
        constants.HYPERV_VM_STATE_PAUSED: virtevent.EVENT_LIFECYCLE_PAUSED,
        constants.HYPERV_VM_STATE_SUSPENDED:
            virtevent.EVENT_LIFECYCLE_SUSPENDED
    }

    def __init__(self, state_change_callback=None):
        self._vmutils = utilsfactory.get_vmutils()
        self._listener = self._vmutils.get_vm_power_state_change_listener(
            timeframe=CONF.hyperv.power_state_check_timeframe,
            event_timeout=CONF.hyperv.power_state_event_polling_interval,
            filtered_states=list(self._TRANSITION_MAP.keys()),
            get_handler=True)

        self._serial_console_ops = serialconsoleops.SerialConsoleOps()
        self._state_change_callback = state_change_callback

    def start_listener(self):
        eventlet.spawn_n(self._listener, self._event_callback)

    def _event_callback(self, instance_name, instance_power_state):
        # Instance uuid set by Nova. If this is missing, we assume that
        # the instance was not created by Nova and ignore the event.
        instance_uuid = self._get_instance_uuid(instance_name)
        if instance_uuid:
            self._emit_event(instance_name,
                             instance_uuid,
                             instance_power_state)

    def _emit_event(self, instance_name, instance_uuid, instance_state):
        virt_event = self._get_virt_event(instance_uuid,
                                          instance_state)
        eventlet.spawn_n(self._state_change_callback, virt_event)

        eventlet.spawn_n(self._handle_serial_console_workers,
                         instance_name, instance_state)

    def _handle_serial_console_workers(self, instance_name, instance_state):
        if instance_state == constants.HYPERV_VM_STATE_ENABLED:
            self._serial_console_ops.start_console_handler(instance_name)
        else:
            self._serial_console_ops.stop_console_handler(instance_name)

    def _get_instance_uuid(self, instance_name):
        try:
            instance_uuid = self._vmutils.get_instance_uuid(instance_name)
            if not instance_uuid:
                LOG.warn(_LW("Instance uuid could not be retrieved for "
                             "instance %s. Instance state change event "
                             "will be ignored."),
                         instance_name)
            return instance_uuid
        except os_win_exc.HyperVVMNotFoundException:
            # The instance has been deleted.
            pass

    def _get_virt_event(self, instance_uuid, instance_state):
        transition = self._TRANSITION_MAP[instance_state]
        return virtevent.LifecycleEvent(uuid=instance_uuid,
                                        transition=transition)
