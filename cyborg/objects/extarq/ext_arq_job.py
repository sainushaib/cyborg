# Copyright 2019 Intel Ltd.
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

from oslo_log import log as logging

from cyborg.common import constants
from cyborg.common.constants import ARQ_STATES_TRANSFORM_MATRIX
from cyborg.common import exception
from cyborg.common import nova_client
from cyborg.common import utils
from cyborg.conf import CONF
from cyborg import objects


LOG = logging.getLogger(__name__)


class ExtARQJobMixin(object):
    """Mixin Class for ExtARQ async job management."""

    def _bind_job(self, context, deployable):
        """The bind process of an acclerator."""
        check_extra_job = getattr(self, "_need_extra_bind_job", None)
        need_job = None
        if check_extra_job:
            need_job = check_extra_job(context, deployable)
        if getattr(self.bind, "is_job", False) and need_job is not False:
            LOG.info("Start job for ARQ(%s) bind.", self.arq.uuid)
            works = utils.ThreadWorks()
            job = works.spawn(self.bind, context, deployable)
            return job
        else:
            LOG.info("ARQ(%s) bind process is instant.", self.arq.uuid)
            self.bind(context, deployable)

    @classmethod
    def get_suitable_ext_arq(cls, context, uuid):
        """From the inherit subclass find the suitable ExtARQ."""
        extarq = cls.get(context, uuid)
        typ, _ = extarq.get_resources_from_device_profile_group()
        factory = cls.factory(typ)
        if factory != cls:
            return factory.get(context, uuid)
        return extarq

    def start_bind_job(self, context, valid_fields):
        """Check and start bind jobs for ARQ."""
        # Check can ARC be bound.
        if (self.arq.state not in
                ARQ_STATES_TRANSFORM_MATRIX[constants.ARQ_BIND_STARTED]):
            raise exception.ARQInvalidState(state=self.arq.state)

        hostname = valid_fields[self.arq.uuid]['hostname']
        devrp_uuid = valid_fields[self.arq.uuid]['device_rp_uuid']
        instance_uuid = valid_fields[self.arq.uuid]['instance_uuid']
        LOG.info('[arqs:objs] bind. hostname: %s, devrp_uuid: %s'
                 'instance: %s', hostname, devrp_uuid, instance_uuid)

        self.arq.hostname = hostname
        self.arq.device_rp_uuid = devrp_uuid
        self.arq.instance_uuid = instance_uuid

        # If prog fails, we'll change this ARQ state changes get committed here
        self.update_check_state(context, constants.ARQ_BIND_STARTED)

        dep = objects.Deployable.get_by_device_rp_uuid(context, devrp_uuid)
        return self._bind_job(context, dep)

    @classmethod
    def master(cls, context, arq_binds):
        """Start a master thread to monitor job workers."""
        jobs = {}
        instant = {}
        arq_uuids = [ea.arq.uuid for ea in arq_binds.keys()]
        for arq_uuid, job in arq_binds.items():
            kv = {arq_uuid: job}
            jobs.update(kv) if job else instant.update(kv)
        if not jobs:
            LOG.info("All ARQ(%s) bind process are instant.", arq_uuids)
            cls.check_bindings_result(context, arq_binds.keys())
            return
        th_workers = utils.ThreadWorks()
        works_generator = th_workers.get_workers_result(
            jobs.values(), timeout=CONF.bind_timeout)
        # arq_binds, timeout=1)
        LOG.info("Check ARQ(%s) bind jobs status.", arq_uuids)
        th_workers.spawn_master(
            cls.job_monitor, context, works_generator, arq_binds.keys())

    @classmethod
    def check_bindings_result(cls, context, extarqs):
        """Check the ARQ bind status result."""
        # Batch get or get one by one? Maybe delete a ARQ
        arq_uuids = [ea.arq.uuid for ea in extarqs]

        extarqs = list(extarqs)
        device_profile_name = extarqs[0].arq.device_profile_name
        instance_uuid = extarqs[0].arq.instance_uuid

        extarqs = cls.list(context, arq_uuids)
        if len(extarqs) < len(arq_uuids):
            LOG.error("ARQs(%s) bind status sync error, status is %s. "
                      "For some ARQs %s are deleted.",
                      arq_uuids, constants.ARQ_BIND_STATUS_FAILED,
                      set(arq_uuids) - set([[ea.arq.uuid for ea in extarqs]]))
            cls.bind_notify(device_profile_name, instance_uuid,
                            constants.ARQ_BIND_STATUS_FAILED)

        status = constants.ARQ_BIND_STATUS_FINISH
        for extarq in extarqs:
            state = extarq.arq.state
            uuid = extarq.arq.uuid
            if state in constants.ARQ_PRE_BIND:
                # OPEN ignore ARQ_OUFOF_BIND_FLOW?
                status = constants.ARQ_BIND_STATUS_FAILED
                LOG.error("ARQs(%s) bind has not finished, status is %s.",
                          uuid, status)
                break
            elif state in constants.ARQ_OUFOF_BIND_FLOW + [
                constants.ARQ_BIND_STATUS_FAILED]:
                # OPEN ignore ARQ_OUFOF_BIND_FLOW?
                status = constants.ARQ_BIND_STATUS_FAILED
                LOG.error("ARQs(%s) bind status sync error, status is %s.",
                          uuid, status)
                break
            elif state == constants.ARQ_BOUND:
                LOG.info("ARQs(%s) bind status sync finish, status is %s.",
                         uuid, status)
        if status == constants.ARQ_BIND_STATUS_FINISH:
            LOG.info('All ARQs %s async bind jobs has finished.', arq_uuids)
        cls.bind_notify(device_profile_name, instance_uuid, status)

    @classmethod
    @utils.wrap_job_tb("Error in ARQ bind async job_monitor. Reason: %s")
    def job_monitor(cls, context, works_generator, extarqs):
        """monitor every deployable bind jobs."""
        # result: f.result(), f.exception_info(), f._state
        msg = None
        arq_uuids = [ea.arq.uuid for ea in extarqs]
        LOG.info('Monitor master check ARQ %s async bind job.', arq_uuids)
        for _, (exc, tb), _, err in works_generator:
            msg = "".join(utils.format_tb(tb)) + str(exc) if exc else err
            if msg:
                LOG.error(msg)
            # TODO(Shaohe) Rollback? Such as We have _update_placement,
            # should cancel it.
        if not arq_uuids:
            return
        cls.check_bindings_result(context, extarqs)

    @classmethod
    def bind_notify(cls, device_profile_name, instance_uuid, status):
        """Notify the bind status to nova."""
        nova_api = nova_client.NovaAPI()
        nova_api.notify_binding(instance_uuid,
                                device_profile_name, status)

    def get_resources_from_device_profile_group(self):
        """parser device profile group."""
        group = self.device_profile_group
        # example: {"resources:CUSTOM_ACCELERATOR_FPGA": "1"}
        resources = [
            (k.lstrip(constants.RESOURCES_PREFIX), v) for k, v in group.items()
            if k.startswith(constants.RESOURCES_PREFIX)]
        if not resources:
            raise exception.InvalidParameterValue(
                'No resources in device_profile_group: %s' % group)
        res_type, res_num = resources[0]
        if res_type not in constants.SUPPORT_RESOURCES:
            raise exception.InvalidParameterValue(
                'Unsupport resources %s from device_profile_group: %s' %
                (res_type, group))
        try:
            res_num = int(res_num)
        except ValueError:
            raise exception.InvalidParameterValue(
                'Resources nummber is a invalid in'
                'device_profile_group: %s' % group)
        return res_type, res_num

    @classmethod
    def apply_patch(cls, context, patch_list, valid_fields):
        """Apply JSON patch. See api/controllers/v1/arqs.py."""
        arq_binds = {}
        for arq_uuid, patch in patch_list.items():
            extarq = cls.get_suitable_ext_arq(context, arq_uuid)
            if patch[0]['op'] == 'add':  # All ops are 'add'
                # arq_notify_list.append(arq_uuid)
                job = extarq.start_bind_job(context, valid_fields)
                arq_binds[extarq] = job
            else:
                extarq.unbind(context, extarq)
        if arq_binds:
            cls.master(context, arq_binds)
