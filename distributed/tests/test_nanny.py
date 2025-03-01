from __future__ import annotations

import asyncio
import gc
import logging
import multiprocessing as mp
import os
import random
import sys
from contextlib import suppress

import psutil
import pytest

pytestmark = pytest.mark.gpu

from tlz import first, valmap
from tornado.ioloop import IOLoop

import dask
from dask.utils import tmpfile

from distributed import Nanny, Scheduler, Worker, profile, rpc, wait, worker
from distributed.compatibility import LINUX, WINDOWS
from distributed.core import CommClosedError, Status
from distributed.diagnostics import SchedulerPlugin
from distributed.metrics import time
from distributed.protocol.pickle import dumps
from distributed.utils import TimeoutError, parse_ports
from distributed.utils_test import (
    captured_logger,
    gen_cluster,
    gen_test,
    raises_with_cause,
)

pytestmark = pytest.mark.ci1


@gen_cluster(Worker=Nanny)
async def test_str(s, a, b):
    assert a.worker_address in str(a)
    assert a.worker_address in repr(a)
    assert str(a.nthreads) in str(a)
    assert str(a.nthreads) in repr(a)


@gen_cluster(nthreads=[], client=True)
async def test_nanny_process_failure(c, s):
    async with Nanny(s.address, nthreads=2) as n:
        first_dir = n.worker_dir

        assert os.path.exists(first_dir)

        ww = rpc(n.worker_address)
        await ww.update_data(data=valmap(dumps, {"x": 1, "y": 2}))
        pid = n.pid
        assert pid is not None
        with suppress(CommClosedError):
            await c.run(os._exit, 0, workers=[n.worker_address])

        while n.pid == pid:  # wait while process dies and comes back
            await asyncio.sleep(0.01)

        await asyncio.sleep(1)
        while not n.is_alive():  # wait while process comes back
            await asyncio.sleep(0.01)

        # assert n.worker_address != original_address  # most likely

        while n.worker_address not in s.workers or n.worker_dir is None:
            await asyncio.sleep(0.01)

        second_dir = n.worker_dir

    await n.close()
    assert not os.path.exists(second_dir)
    assert not os.path.exists(first_dir)
    assert first_dir != n.worker_dir
    await ww.close_rpc()
    s.stop()


@gen_cluster(nthreads=[])
async def test_run(s):
    async with Nanny(s.address, nthreads=2) as n:
        async with rpc(n.address) as nn:
            response = await nn.run(function=dumps(lambda: 1))
            assert response["status"] == "OK"
            assert response["result"] == 1


@pytest.mark.slow
@gen_cluster(config={"distributed.comm.timeouts.connect": "1s"}, timeout=120)
async def test_no_hang_when_scheduler_closes(s, a, b):
    # https://github.com/dask/distributed/issues/2880
    with captured_logger("tornado.application", logging.ERROR) as logger:
        await s.close()
        await asyncio.sleep(1.2)
        assert a.status == Status.closed
        assert b.status == Status.closed

    out = logger.getvalue()
    assert "Timed out trying to connect" not in out


@pytest.mark.slow
@gen_cluster(
    Worker=Nanny, nthreads=[("127.0.0.1", 1)], worker_kwargs={"reconnect": False}
)
async def test_close_on_disconnect(s, w):
    await s.close()

    start = time()
    while w.status != Status.closed:
        await asyncio.sleep(0.05)
        assert time() < start + 9


class Something(Worker):
    # a subclass of Worker which is not Worker
    pass


@gen_cluster(client=True, Worker=Nanny)
async def test_nanny_worker_class(c, s, w1, w2):
    out = await c._run(lambda dask_worker=None: str(dask_worker.__class__))
    assert "Worker" in list(out.values())[0]
    assert w1.Worker is Worker


@gen_cluster(client=True, Worker=Nanny, worker_kwargs={"worker_class": Something})
async def test_nanny_alt_worker_class(c, s, w1, w2):
    out = await c._run(lambda dask_worker=None: str(dask_worker.__class__))
    assert "Something" in list(out.values())[0]
    assert w1.Worker is Something


@pytest.mark.slow
@gen_cluster(nthreads=[])
async def test_nanny_death_timeout(s):
    await s.close()
    w = Nanny(s.address, death_timeout=1)
    with pytest.raises(TimeoutError):
        await w

    assert w.status == Status.failed


@gen_cluster(client=True, Worker=Nanny)
async def test_random_seed(c, s, a, b):
    async def check_func(func):
        x = c.submit(func, 0, 2**31, pure=False, workers=a.worker_address)
        y = c.submit(func, 0, 2**31, pure=False, workers=b.worker_address)
        assert x.key != y.key
        x = await x
        y = await y
        assert x != y

    await check_func(lambda a, b: random.randint(a, b))
    np = pytest.importorskip("numpy")
    await check_func(lambda a, b: np.random.randint(a, b))


@pytest.mark.skipif(WINDOWS, reason="num_fds not supported on windows")
@gen_cluster(nthreads=[])
async def test_num_fds(s):
    proc = psutil.Process()

    # Warm up
    async with Nanny(s.address):
        pass
    with profile.lock:
        gc.collect()

    before = proc.num_fds()

    for i in range(3):
        async with Nanny(s.address):
            await asyncio.sleep(0.1)

    while proc.num_fds() > before:
        print("fds:", before, proc.num_fds())
        await asyncio.sleep(0.1)


@pytest.mark.skipif(not LINUX, reason="Need 127.0.0.2 to mean localhost")
@gen_cluster(client=True, nthreads=[])
async def test_worker_uses_same_host_as_nanny(c, s):
    for host in ["tcp://0.0.0.0", "tcp://127.0.0.2"]:
        async with Nanny(s.address, host=host):

            def func(dask_worker):
                return dask_worker.listener.listen_address

            result = await c.run(func)
            assert host in first(result.values())


@gen_test()
async def test_scheduler_file():
    with tmpfile() as fn:
        s = await Scheduler(scheduler_file=fn, dashboard_address=":0")
        async with Nanny(scheduler_file=fn) as n:
            assert set(s.workers) == {n.worker_address}
        s.stop()


@gen_cluster(client=True, Worker=Nanny, nthreads=[("127.0.0.1", 2)])
async def test_nanny_timeout(c, s, a):
    x = await c.scatter(123)
    with captured_logger(
        logging.getLogger("distributed.nanny"), level=logging.ERROR
    ) as logger:
        response = await a.restart(timeout=0.1)

    out = logger.getvalue()
    assert "timed out" in out.lower()

    start = time()
    while x.status != "cancelled":
        await asyncio.sleep(0.1)
        assert time() < start + 7


@gen_cluster(
    nthreads=[("", 1)] * 8,
    client=True,
    clean_kwargs={"threads": False},
    config={"distributed.worker.memory.pause": False},
)
async def test_throttle_outgoing_connections(c, s, a, *other_workers):
    # Put a bunch of small data on worker a
    logging.getLogger("distributed.worker").setLevel(logging.DEBUG)
    remote_data = c.map(
        lambda x: b"0" * 10000, range(10), pure=False, workers=[a.address]
    )
    await wait(remote_data)

    a.status = Status.paused
    a.outgoing_current_count = 2

    requests = [
        await a.get_data(await w.rpc.connect(w.address), keys=[f.key], who=w.address)
        for w in other_workers
        for f in remote_data
    ]
    await wait(requests)
    wlogs = await c.get_worker_logs(workers=[a.address])
    wlogs = "\n".join(x[1] for x in wlogs[a.address])
    assert "throttling" in wlogs.lower()


@gen_cluster(nthreads=[])
async def test_scheduler_address_config(s):
    with dask.config.set({"scheduler-address": s.address}):
        async with Nanny() as nanny:
            assert nanny.scheduler.address == s.address
            while not s.workers:
                await asyncio.sleep(0.01)


@pytest.mark.slow
@gen_test()
async def test_wait_for_scheduler():
    with captured_logger("distributed") as log:
        w = Nanny("127.0.0.1:44737")
        IOLoop.current().add_callback(w.start)
        await asyncio.sleep(6)
        await w.close()

    log = log.getvalue()
    assert "error" not in log.lower(), log
    assert "restart" not in log.lower(), log


@gen_cluster(client=True, nthreads=[])
async def test_environment_variable(c, s):
    with dask.config.set({"distributed.nanny.environ": "456"}):
        with pytest.raises(TypeError, match="configuration must be of type dict"):
            Nanny(s.address)

    try:
        # precedence: kwargs > env var > config
        with dask.config.set({"distributed.nanny.environ": {"FOO": "123"}}):
            os.environ["FOO"] = "456"
            async with Nanny(s.address, env={"FOO": "789"}) as n:
                results = await c.run(lambda: os.environ["FOO"])
                assert results == {n.worker_address: "789"}

            # Nanny sets the env variables in the Nanny's own process.
            # Need to manually clean up os.environ afterwards.
            os.environ["FOO"] = "456"
            async with Nanny(s.address) as n:
                results = await c.run(lambda: os.environ["FOO"])
                assert results == {n.worker_address: "456"}

            del os.environ["FOO"]
            async with Nanny(s.address) as n:
                results = await c.run(lambda: os.environ["FOO"])
                assert results == {n.worker_address: "123"}
    finally:
        del os.environ["FOO"]


@gen_cluster(client=True, nthreads=[])
async def test_environment_variable_race_condition(c, s):
    """Nannies need to set the environment variables in their own process so that
    special variables such as MALLOC_TRIM_THRESHOLD_ are picked up when the Worker's
    interpreter is started. However, this creates a race condition with multiple Nannies
    in the same process with different variables. Test that the environment variables
    are set again inside their own process."""
    nannies = [Nanny(s.address, env={"FOO": str(i)}) for i in range(8)]
    try:
        await asyncio.gather(*nannies)
        results = await c.run(lambda: os.environ["FOO"])
        assert sorted(results.values()) == [str(i) for i in range(8)]
        await asyncio.gather(*(n.close() for n in nannies))
    finally:
        del os.environ["FOO"]


@gen_cluster(
    nthreads=[],
    client=True,
    config={"distributed.nanny.environ": {"A": 1, "B": 2, "D": 4}},
)
async def test_environment_variable_config(c, s, monkeypatch):
    monkeypatch.setenv("D", "123")
    async with Nanny(s.address, env={"B": 3, "C": 4}) as n:
        results = await c.run(lambda: os.environ)
        assert results[n.worker_address]["A"] == "1"
        assert results[n.worker_address]["B"] == "3"
        assert results[n.worker_address]["C"] == "4"
        assert results[n.worker_address]["D"] == "123"


@gen_cluster(nthreads=[])
async def test_local_directory(s):
    with tmpfile() as fn:
        with dask.config.set(temporary_directory=fn):
            async with Nanny(s.address) as n:
                assert n.local_directory.startswith(fn)
                assert "dask-worker-space" in n.local_directory
                assert n.process.worker_dir.count("dask-worker-space") == 1


def _noop(x):
    """Define here because closures aren't pickleable."""
    pass


@gen_cluster(
    nthreads=[("127.0.0.1", 1)],
    client=True,
    Worker=Nanny,
    config={"distributed.worker.daemon": False},
)
async def test_mp_process_worker_no_daemon(c, s, a):
    def multiprocessing_worker():
        p = mp.Process(target=_noop, args=(None,))
        p.start()
        p.join()

    await c.submit(multiprocessing_worker)


@gen_cluster(
    nthreads=[("127.0.0.1", 1)],
    client=True,
    Worker=Nanny,
    config={"distributed.worker.daemon": False},
)
async def test_mp_pool_worker_no_daemon(c, s, a):
    def pool_worker(world_size):
        with mp.Pool(processes=world_size) as p:
            p.map(_noop, range(world_size))

    await c.submit(pool_worker, 4)


@gen_cluster(nthreads=[])
async def test_nanny_closes_cleanly(s):
    async with Nanny(s.address) as n:
        assert n.process.pid
        proc = n.process.process
    assert not n.process
    assert not proc.is_alive()
    assert proc.exitcode == 0


@pytest.mark.slow
@gen_cluster(nthreads=[], timeout=60)
async def test_lifetime(s):
    counter = 0
    event = asyncio.Event()

    class Plugin(SchedulerPlugin):
        def add_worker(self, **kwargs):
            pass

        def remove_worker(self, **kwargs):
            nonlocal counter
            counter += 1
            if counter == 2:  # wait twice, then trigger closing event
                event.set()

    s.add_plugin(Plugin())
    async with Nanny(s.address):
        async with Nanny(s.address, lifetime="500 ms", lifetime_restart=True):
            await event.wait()


@gen_cluster(client=True, nthreads=[])
async def test_nanny_closes_cleanly_if_worker_is_terminated(c, s):
    async with Nanny(s.address) as n:
        async with c.rpc(n.worker_address) as w:
            IOLoop.current().add_callback(w.terminate)
            start = time()
            while n.status != Status.closed:
                await asyncio.sleep(0.01)
                assert time() < start + 5

            assert n.status == Status.closed


@gen_cluster(client=True, nthreads=[])
async def test_config(c, s):
    async with Nanny(s.address, config={"foo": "bar"}) as n:
        config = await c.run(dask.config.get, "foo")
        assert config[n.worker_address] == "bar"


@gen_cluster(client=True, nthreads=[])
async def test_nanny_port_range(c, s):
    nanny_port = "9867:9868"
    worker_port = "9869:9870"
    async with Nanny(s.address, port=nanny_port, worker_port=worker_port) as n1:
        assert n1.port == 9867  # Selects first port in range
        async with Nanny(s.address, port=nanny_port, worker_port=worker_port) as n2:
            assert n2.port == 9868  # Selects next port in range
            with raises_with_cause(
                RuntimeError,
                "Nanny failed to start.",
                ValueError,
                "with port 9867:9868",
            ):  # No more ports left
                async with Nanny(s.address, port=nanny_port, worker_port=worker_port):
                    pass

            # Ensure Worker ports are in worker_port range
            def get_worker_port(dask_worker):
                return dask_worker.port

            worker_ports = await c.run(get_worker_port)
            assert list(worker_ports.values()) == parse_ports(worker_port)


class KeyboardInterruptWorker(worker.Worker):
    """A Worker that raises KeyboardInterrupt almost immediately"""

    async def heartbeat(self):
        def raise_err():
            raise KeyboardInterrupt()

        self.loop.add_callback(raise_err)


@pytest.mark.parametrize("protocol", ["tcp", "ucx"])
@gen_test()
async def test_nanny_closed_by_keyboard_interrupt(ucx_loop, protocol):
    if protocol == "ucx":  # Skip if UCX isn't available
        pytest.importorskip("ucp")

    async with Scheduler(protocol=protocol, dashboard_address=":0") as s:
        async with Nanny(
            s.address, nthreads=1, worker_class=KeyboardInterruptWorker
        ) as n:
            await n.process.stopped.wait()
            # Check that the scheduler has been notified about the closed worker
            assert "remove-worker" in str(s.events)


class StartException(Exception):
    pass


class BrokenWorker(worker.Worker):
    async def start(self):
        raise StartException("broken")


@gen_cluster(nthreads=[])
async def test_worker_start_exception(s):
    # make sure this raises the right Exception:
    with raises_with_cause(RuntimeError, None, StartException, None):
        async with Nanny(s.address, worker_class=BrokenWorker) as n:
            pass


@gen_cluster(nthreads=[])
async def test_failure_during_worker_initialization(s):
    with captured_logger(logger="distributed.nanny", level=logging.WARNING) as logs:
        with pytest.raises(Exception):
            async with Nanny(s.address, foo="bar") as n:
                await n
    assert "Restarting worker" not in logs.getvalue()


@gen_cluster(client=True, Worker=Nanny)
async def test_environ_plugin(c, s, a, b):
    from dask.distributed import Environ

    await c.register_worker_plugin(Environ({"ABC": 123}))

    async with Nanny(s.address, name="new") as n:
        results = await c.run(os.getenv, "ABC")
        assert results[a.worker_address] == "123"
        assert results[b.worker_address] == "123"
        assert results[n.worker_address] == "123"


@pytest.mark.parametrize(
    "modname",
    [
        # numpy is always imported, and for a good reason:
        # https://github.com/dask/distributed/issues/5729
        "scipy",
        pytest.param("pandas", marks=pytest.mark.xfail(reason="distributed#5723")),
    ],
)
@gen_cluster(client=True, Worker=Nanny, nthreads=[("", 1)])
async def test_no_unnecessary_imports_on_worker(c, s, a, modname):
    """
    Regression test against accidentally importing unnecessary modules at worker startup.

    Importing modules like pandas slows down worker startup, especially if workers are
    loading their software environment from NFS or other non-local filesystems.
    It also slightly increases memory footprint.
    """

    def assert_no_import(dask_worker):
        assert modname not in sys.modules

    await c.wait_for_workers(1)
    await c.run(assert_no_import)


@pytest.mark.slow
@gen_cluster(client=True, Worker=Nanny)
async def test_repeated_restarts(c, s, a, b):
    for _ in range(3):
        await c.restart()
        assert len(s.workers) == 2


@pytest.mark.slow
@gen_cluster(
    client=True,
    Worker=Nanny,
    worker_kwargs={"memory_limit": "1 GiB"},
    nthreads=[("127.0.0.1", 1)],
)
async def test_restart_memory(c, s, n):
    # First kill nanny with restart
    await c.restart()

    # then kill nanny with memory
    from dask.distributed import KilledWorker

    np = pytest.importorskip("numpy")
    s.allowed_failures = 1
    future = c.submit(np.ones, 300_000_000, dtype="f8")
    with pytest.raises(KilledWorker):
        await future

    while not s.workers:
        await asyncio.sleep(0.1)


@gen_cluster(Worker=Nanny, nthreads=[("", 1)])
async def test_scheduler_crash_doesnt_restart(s, a):
    # Simulate a scheduler crash by disconnecting it first
    # (`s.close()` would tell workers to cleanly shut down)
    bcomm = next(iter(s.stream_comms.values()))
    bcomm.abort()
    await s.close()

    while a.status not in {Status.closing_gracefully, Status.closed}:
        await asyncio.sleep(0.01)

    await a.finished()
    assert a.status == Status.closed
    assert a.process is None


@pytest.mark.slow
@pytest.mark.skipif(not LINUX, reason="Requires GNU libc")
@gen_cluster(
    client=True,
    Worker=Nanny,
    nthreads=[("", 2)],
    worker_kwargs={"memory_limit": "2GiB"},
)
async def test_malloc_trim_threshold(c, s, a):
    """Test that the nanny sets the MALLOC_TRIM_THRESHOLD_ environment variable before
    starting the worker process.

    This test relies on these settings to work:

        distributed.nanny.environ.MALLOC_TRIM_THRESHOLD_: 65536
        distributed.worker.multiprocessing-method: spawn

    We're deliberately not setting them explicitly in @gen_cluster above, as we want
    this test to trip if somebody changes distributed.yaml.

    Note
    ----
    This test may start failing in a future Python version if CPython switches to
    using mimalloc by default. If it does, a thorough benchmarking exercise is needed.
    """
    da = pytest.importorskip("dask.array")

    a = da.random.random(
        2**29 // 8,  # 0.5 GiB,
        chunks=160 * 2**10 // 8,  # 160 kiB
    ).persist()
    await wait(a)
    # Wait for heartbeat
    while s.memory.process < 2**29:
        await asyncio.sleep(0.01)
    del a

    # This is the delicate bit, as it relies on
    # 1. PyMem_Free() to be quick to invoke glibc free() when memory becomes available
    # 2. glibc free() to be quick to invoke the kernel's sbrk() when the same happens
    #
    # At the moment of writing, the readings are:
    # - 122 MiB after starting a new worker
    # - 139 MiB after computing a trivial dask.array collection
    # - 185 MiB at the end of this test, with MALLOC_TRIM_THRESHOLD_=65536
    # - 698 MiB at the end of this test, without MALLOC_TRIM_THRESHOLD_
    while s.memory.process > 250 * 2**20:
        await asyncio.sleep(0.01)
