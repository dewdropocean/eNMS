from copy import deepcopy
from datetime import datetime
from logging import info
from multiprocessing.pool import ThreadPool
from napalm import get_network_driver
from napalm.base.base import NetworkDriver
from netmiko import ConnectHandler
from os import environ
from paramiko import SSHClient
from re import compile, search
from scp import SCPClient
from sqlalchemy import Boolean, Column, ForeignKey, Integer, PickleType, String
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import backref, relationship
from time import sleep
from typing import Any, List, Optional, Set, Tuple

from eNMS.main import db
from eNMS.base.associations import (
    job_device_table,
    job_log_rule_table,
    job_pool_table,
    job_workflow_table,
)
from eNMS.base.helpers import fetch
from eNMS.base.models import Base
from eNMS.inventory.models import Device


class Job(Base):

    __tablename__ = "Job"
    type = Column(String)
    __mapper_args__ = {"polymorphic_identity": "Job", "polymorphic_on": type}
    id = Column(Integer, primary_key=True)
    hidden = Column(Boolean, default=False)
    name = Column(String, unique=True)
    description = Column(String)
    multiprocessing = Column(Boolean, default=False)
    max_processes = Column(Integer, default=50)
    number_of_retries = Column(Integer, default=0)
    time_between_retries = Column(Integer, default=10)
    positions = Column(MutableDict.as_mutable(PickleType), default={})
    logs = Column(MutableDict.as_mutable(PickleType), default={})
    status = Column(String, default="Idle")
    state = Column(MutableDict.as_mutable(PickleType), default={})
    credentials = Column(String, default="device")
    tasks = relationship("Task", back_populates="job", cascade="all,delete")
    vendor = Column(String)
    operating_system = Column(String)
    waiting_time = Column(Integer, default=0)
    creator_id = Column(Integer, ForeignKey("User.id"))
    creator = relationship("User", back_populates="jobs")
    push_to_git = Column(Boolean, default=False)
    workflows = relationship(
        "Workflow", secondary=job_workflow_table, back_populates="jobs"
    )
    devices = relationship("Device", secondary=job_device_table, back_populates="jobs")
    pools = relationship("Pool", secondary=job_pool_table, back_populates="jobs")
    log_rules = relationship(
        "LogRule", secondary=job_log_rule_table, back_populates="jobs"
    )
    send_notification = Column(Boolean, default=False)
    send_notification_method = Column(String, default="mail_feedback_notification")
    display_only_failed_nodes = Column(Boolean, default=True)
    mail_recipient = Column(String, default="")

    @property
    def creator_name(self) -> str:
        return self.creator.name if self.creator else "None"

    def compute_targets(self) -> Set[Device]:
        targets = set(self.devices)
        for pool in self.pools:
            targets |= set(pool.devices)
        return targets

    def job_sources(self, workflow: "Workflow", subtype: str = "all") -> List["Job"]:
        return [
            x.source
            for x in self.sources
            if (subtype == "all" or x.subtype == subtype) and x.workflow == workflow
        ]

    def job_successors(self, workflow: "Workflow", subtype: str = "all") -> List["Job"]:
        return [
            x.destination
            for x in self.destinations
            if (subtype == "all" or x.subtype == subtype) and x.workflow == workflow
        ]

    def build_notification(self, results: dict, now: str) -> str:
        summary = [
            f"Job: {self.name} ({self.type})",
            f"Runtime: {now}",
            f'Status: {"PASS" if results["success"] else "FAILED"}',
        ]
        if "devices" in results.get("result", "") and not results["success"]:
            failed = "\n".join(
                device
                for device, logs in results["result"]["devices"].items()
                if not logs["success"]
            )
            summary.append(f"FAILED\n{failed}")
            if not self.display_only_failed_nodes:
                passed = "\n".join(
                    device
                    for device, logs in results["result"]["devices"].items()
                    if logs["success"]
                )
                summary.append(f"\n\nPASS:\n{passed}")
        server_url = environ.get("ENMS_SERVER_ADDR", "http://SERVER_IP")
        logs_url = f"{server_url}/automation/logs/{self.id}/{now}"
        summary.append(f"Logs: {logs_url}")
        return "\n\n".join(summary)

    def notify(self, results: dict, time: str) -> None:
        fetch("Job", name=self.send_notification_method).try_run(
            {
                "job": self.serialized,
                "logs": self.logs,
                "runtime": time,
                "result": self.build_notification(results, time),
            }
        )

    def try_run(
        self,
        payload: Optional[dict] = None,
        targets: Optional[Set[Device]] = None,
        from_workflow: bool = False,
    ) -> Tuple[dict, str]:
        self.status, self.state = "Running", {}
        if not payload:
            payload = {}
        info(f"{self.name}: starting.")
        if not from_workflow:
            db.session.commit()
        failed_attempts, now = {}, str(datetime.now()).replace(" ", "-")
        for i in range(self.number_of_retries + 1):
            info(f"Running job {self.name}, attempt {i}")
            results, targets = self.run(payload, targets)
            if results["success"]:
                break
            if i != self.number_of_retries:
                failed_attempts[f"Attempts {i + 1}"] = results
                sleep(self.time_between_retries)
        results["failed_attempts"] = failed_attempts
        self.logs[now] = results
        info(f"{self.name}: finished.")
        self.status, self.state = "Idle", {}
        if not from_workflow:
            db.session.commit()
            if self.send_notification:
                self.notify(results, now)
        return results, now

    def get_results(self, payload: dict, device: Optional[Device] = None) -> dict:
        try:
            return self.job(payload, device) if device else self.job(payload)
        except Exception as e:
            return {"success": False, "result": str(e)}

    def device_run(self, args: Tuple[Device, dict, dict]) -> None:
        device, results, payload = args
        device_result = self.get_results(payload, device)
        results["result"]["devices"][device.name] = device_result

    def run(
        self, payload: dict, targets: Optional[Set[Device]] = None
    ) -> Tuple[dict, Optional[Set[Device]]]:
        if not targets and getattr(self, "use_workflow_targets", True):
            targets = self.compute_targets()
        if targets:
            results: dict = {"result": {"devices": {}}}
            if self.multiprocessing:
                processes = min(len(targets), self.max_processes)
                pool = ThreadPool(processes=processes)
                pool.map(
                    self.device_run, [(device, results, payload) for device in targets]
                )
                pool.close()
                pool.join()
            else:
                results["result"]["devices"] = {
                    device.name: self.get_results(payload, device) for device in targets
                }
            remaining_targets = {
                device
                for device in targets
                if not results["result"]["devices"][device.name]["success"]
            }
            results["success"] = not bool(remaining_targets)
            return results, remaining_targets
        else:
            return self.get_results(payload), None


class Service(Job):

    __tablename__ = "Service"
    id = Column(Integer, ForeignKey("Job.id"), primary_key=True)
    __mapper_args__ = {"polymorphic_identity": "Service"}

    def get_credentials(self, device: Device) -> Tuple[str, str]:
        return (
            (self.creator.name, self.creator.password)
            if self.credentials == "user"
            else (device.username, device.password)
        )

    def netmiko_connection(self, device: Device) -> ConnectHandler:
        username, password = self.get_credentials(device)
        return ConnectHandler(
            device_type=(
                device.netmiko_driver if self.use_device_driver else self.driver
            ),
            ip=device.ip_address,
            username=username,
            password=password,
            secret=device.enable_password,
            fast_cli=self.fast_cli,
            timeout=self.timeout,
            global_delay_factor=self.global_delay_factor,
        )

    def napalm_connection(self, device: Device) -> NetworkDriver:
        username, password = self.get_credentials(device)
        optional_args = self.optional_args
        if not optional_args:
            optional_args = {}
        if "secret" not in optional_args:
            optional_args["secret"] = device.enable_password
        driver = get_network_driver(
            device.napalm_driver if self.use_device_driver else self.driver
        )
        return driver(
            hostname=device.ip_address,
            username=username,
            password=password,
            optional_args=optional_args,
        )

    def sub(self, data: str, variables: dict) -> str:
        r = compile("{{(.*?)}}")

        def replace_with_locals(match: Any) -> str:
            return str(eval(match.group()[2:-2], variables))

        return r.sub(replace_with_locals, data)

    def space_deleter(self, input: str) -> str:
        return "".join(input.split())

    def match_content(self, result: str, match: str) -> bool:
        if self.delete_spaces_before_matching:
            match, result = map(self.space_deleter, (match, result))
        success = (
            self.content_match_regex
            and bool(search(match, result))
            or match in result
            and not self.content_match_regex
        )
        return success if not self.negative_logic else not success

    def match_dictionnary(self, result: dict, match: Optional[dict] = None) -> bool:
        if self.validation_method == "dict_equal":
            return result == self.dict_match
        else:
            if match is None:
                match = deepcopy(self.dict_match)
            for k, v in result.items():
                if isinstance(v, dict):
                    self.match_dictionnary(v, match)
                elif k in match and match[k] == v:
                    match.pop(k)
            return not match

    def transfer_file(
        self, ssh_client: SSHClient, source: Device, destination: Device
    ) -> None:
        files = (source, destination)
        if self.protocol == "sftp":
            sftp = ssh_client.open_sftp()
            getattr(sftp, self.direction)(*files)
            sftp.close()
        else:
            with SCPClient(ssh_client.get_transport()) as scp:
                getattr(scp, self.direction)(*files)


class WorkflowEdge(Base):

    __tablename__ = type = "WorkflowEdge"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    subtype = Column(String)
    source_id = Column(Integer, ForeignKey("Job.id"))
    source = relationship(
        "Job",
        primaryjoin="Job.id == WorkflowEdge.source_id",
        backref=backref("destinations", cascade="all, delete-orphan"),
        foreign_keys="WorkflowEdge.source_id",
    )
    destination_id = Column(Integer, ForeignKey("Job.id"))
    destination = relationship(
        "Job",
        primaryjoin="Job.id == WorkflowEdge.destination_id",
        backref=backref("sources", cascade="all, delete-orphan"),
        foreign_keys="WorkflowEdge.destination_id",
    )
    workflow_id = Column(Integer, ForeignKey("Workflow.id"))
    workflow = relationship(
        "Workflow", back_populates="edges", foreign_keys="WorkflowEdge.workflow_id"
    )


class Workflow(Job):

    __tablename__ = "Workflow"
    __mapper_args__ = {"polymorphic_identity": "Workflow"}
    id = Column(Integer, ForeignKey("Job.id"), primary_key=True)
    use_workflow_targets = Column(Boolean, default=True)
    last_modified = Column(String)
    jobs = relationship("Job", secondary=job_workflow_table, back_populates="workflows")
    edges = relationship("WorkflowEdge", back_populates="workflow")

    def __init__(self, **kwargs: Any) -> None:
        end = fetch("Service", name="End")
        default = [fetch("Service", name="Start"), end]
        self.jobs.extend(default)
        super().__init__(**kwargs)
        if self.name not in end.positions:
            end.positions[self.name] = (500, 0)

    def job(self, payload: dict, device: Optional[Device] = None) -> dict:
        if not self.multiprocessing:
            self.state = {"jobs": {}}
            if device:
                self.state["current_device"] = device.name
            db.session.commit()
        jobs: List[Job] = [self.jobs[0]]
        visited: Set = set()
        results: dict = {"success": False}
        while jobs:
            job = jobs.pop()
            if any(
                node not in visited for node in job.job_sources(self, "prerequisite")
            ):
                continue
            visited.add(job)
            if not self.multiprocessing:
                self.state["current_job"] = job.get_properties()
                db.session.commit()
            log = f"Workflow {self.name}: job {job.name}"
            if device:
                log += f" on {device.name}"
            info(log)
            job_results, _ = job.try_run(
                results, {device} if device else None, from_workflow=True
            )
            success = job_results["success"]
            if not self.multiprocessing:
                self.state["jobs"][job.id] = success
                db.session.commit()
            edge_type_to_follow = "success" if success else "failure"
            for successor in job.job_successors(self, edge_type_to_follow):
                if successor not in visited:
                    jobs.append(successor)
                if successor == self.jobs[1]:
                    results["success"] = True
            results[job.name] = job_results
            sleep(job.waiting_time)
        return results
