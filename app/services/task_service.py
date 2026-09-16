"""Task Service — Section 11. Task CRUD, lifecycle/state transitions
(Section 26), enqueues Task Runs. Real implementation: MA3."""

from app.services.base import BaseService


class TaskService(BaseService):
    def create_task(self, *args, **kwargs):
        raise NotImplementedError("TaskService lands in MA3")

    def start_task_run(self, *args, **kwargs):
        raise NotImplementedError("TaskService lands in MA3")

    def cancel_task_run(self, *args, **kwargs):
        raise NotImplementedError("TaskService lands in MA3")
