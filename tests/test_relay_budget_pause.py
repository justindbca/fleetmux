"""Lid-closed time not counted against a task's time budget (relay design R25): deferred.

This round does not change the budget: every deadline is to be counted in awake time by the awake-clock work, and
the task budget pause is built on it afterwards. Where the budget is computed today: ``fleet.task_deadline`` (the
hours budget, fx:task ``deadline`` set by ``Fleet.submit``), read by the budget_exhausted checks of ``Fleet.tick``
and ``RuntimeV2.tick`` and stamped into each task delivery as ``deadline_at`` by ``RuntimeV2.queue``."""
import pytest

pytest.skip('deferred: the task time budget pause while the machine sleeps waits for the awake-clock work',
            allow_module_level=True)
