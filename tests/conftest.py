"""Shared fixtures."""
import time

import pytest

from omegaeval.baselines import BaselineAgent, spec_for
from omegaeval.config import Config
from omegaeval.episode import EpisodeRunner
from omegaeval.gateway import Gateway
from omegaeval.store import Store


@pytest.fixture()
def rig(tmp_path):
    """A store, a gateway and a runner on free ports, and baseline(policy) to start a reference agent."""
    cfg = Config(path=None, store=tmp_path / "store", gateway_host="127.0.0.1", gateway_port=0, grace_s=0.5,
                 step_timeout_s=20, read_timeout_s=10, pace_s=0.05)
    store = Store(cfg.store)
    gw = Gateway("127.0.0.1", 0).start()
    runner = EpisodeRunner(cfg, store, gw)
    started = []

    def baseline(pol):
        spec, policy = spec_for(pol)
        cfg.agents[spec.id] = spec
        gw.add(spec.id, spec.token)
        runner.hooks[spec.id] = policy.prepare
        a = BaselineAgent(spec.id, f"ws://127.0.0.1:{gw.port}/agents/{spec.id}", spec.token, policy)
        a.start()
        a.ready.wait(5)
        started.append(a)
        time.sleep(0.1)
        return spec.id

    yield cfg, store, gw, runner, baseline
    for a in started:
        a.stop()
    gw.stop()
