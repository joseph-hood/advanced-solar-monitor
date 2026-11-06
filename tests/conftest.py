import logging

import pytest

from eg4mon.model.lfp_soc_model import ModelParams
from eg4mon.pipeline.estimator_service import EstimatorService, PipelineConfig
from eg4mon.sim import simulate
from eg4mon.sources.base import Sample
from eg4mon.store.db import Store

logging.getLogger("eg4mon").setLevel(logging.WARNING)

T0 = 1_767_000_000.0  # 2025-12-29 UTC


@pytest.fixture
def params():
    return ModelParams()


@pytest.fixture
def store():
    return Store.open(":memory:")


@pytest.fixture
def make_service(params):
    def _make(store, cfg=None, tz="UTC"):
        return EstimatorService(store, params, cfg or PipelineConfig(), tz)
    return _make


@pytest.fixture(scope="session")
def sim_rows():
    return simulate(T0, days=5, tz="America/Chicago")


def sample(ts, v=53.0, p=0.0, **kw):
    return Sample(ts=ts, battery_v=v, battery_p=p, **kw)
