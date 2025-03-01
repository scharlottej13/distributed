from __future__ import annotations

import asyncio
import os
import random
from contextlib import suppress
from time import sleep
from unittest import mock

import pytest
from tlz import first, partition_all

from dask import delayed

from distributed import Client, Nanny, profile, wait
from distributed.comm import CommClosedError
from distributed.compatibility import MACOS
from distributed.metrics import time
from distributed.utils import CancelledError, sync
from distributed.utils_test import (
    BlockedGatherDep,
    captured_logger,
    cluster,
    div,
    gen_cluster,
    inc,
    slowadd,
    slowinc,
)
from distributed.worker_state_machine import FreeKeysEvent

pytestmark = pytest.mark.ci1


@pytest.mark.slow()
def test_submit_after_failed_worker_sync(loop):
    with cluster() as (s, [a, b]):
        with Client(s["address"], loop=loop) as c:
            L = c.map(inc, range(10))
            wait(L)
            a["proc"]().terminate()
            total = c.submit(sum, L)
            assert total.result() == sum(map(inc, range(10)))


@pytest.mark.slow()
@pytest.mark.parametrize("compute_on_failed", [False, True])
@gen_cluster(client=True, config={"distributed.comm.timeouts.connect": "500ms"})
async def test_submit_after_failed_worker_async(c, s, a, b, compute_on_failed):
    async with Nanny(s.address, nthreads=2) as n:
        await c.wait_for_workers(3)

        L = c.map(inc, range(10))
        await wait(L)

        kill_task = asyncio.create_task(n.kill())
        compute_addr = n.worker_address if compute_on_failed else a.address
        total = c.submit(sum, L, workers=[compute_addr], allow_other_workers=True)
        assert await total == sum(range(1, 11))
        await kill_task


@gen_cluster(client=True, timeout=60)
async def test_submit_after_failed_worker(c, s, a, b):
    L = c.map(inc, range(10))
    await wait(L)

    await a.close()
    total = c.submit(sum, L)
    assert await total == sum(range(1, 11))


@pytest.mark.slow
def test_gather_after_failed_worker(loop):
    with cluster() as (s, [a, b]):
        with Client(s["address"], loop=loop) as c:
            L = c.map(inc, range(10))
            wait(L)
            a["proc"]().terminate()
            result = c.gather(L)
            assert result == list(map(inc, range(10)))


@pytest.mark.slow
@gen_cluster(client=True, Worker=Nanny, nthreads=[("127.0.0.1", 1)] * 4, timeout=60)
async def test_gather_then_submit_after_failed_workers(c, s, w, x, y, z):
    L = c.map(inc, range(20))
    await wait(L)

    w.process.process._process.terminate()
    total = c.submit(sum, L)

    for _ in range(3):
        await wait(total)
        addr = first(s.tasks[total.key].who_has).address
        for worker in [x, y, z]:
            if worker.worker_address == addr:
                worker.process.process._process.terminate()
                break

        result = await c.gather([total])
        assert result == [sum(map(inc, range(20)))]


@gen_cluster(Worker=Nanny, client=True, timeout=60)
async def test_restart(c, s, a, b):
    x = c.submit(inc, 1)
    y = c.submit(inc, x)
    z = c.submit(div, 1, 0)
    await y

    assert s.tasks[x.key].state == "memory"
    assert s.tasks[y.key].state == "memory"
    assert s.tasks[z.key].state != "memory"

    f = await c.restart()
    assert f is c

    assert len(s.workers) == 2
    assert not any(ws.occupancy for ws in s.workers.values())

    assert not s.tasks

    assert x.cancelled()
    assert y.cancelled()
    assert z.cancelled()

    assert not s.tasks
    assert not any(cs.wants_what for cs in s.clients.values())


@gen_cluster(Worker=Nanny, client=True, timeout=60)
async def test_restart_cleared(c, s, a, b):
    x = 2 * delayed(1) + 1
    f = c.compute(x)
    await wait([f])

    await c.restart()

    for coll in [s.tasks, s.unrunnable]:
        assert not coll


def test_restart_sync(loop):
    with cluster(nanny=True) as (s, [a, b]):
        with Client(s["address"], loop=loop) as c:
            x = c.submit(div, 1, 2)
            x.result()

            assert sync(loop, c.scheduler.who_has)
            c.restart()
            assert not sync(loop, c.scheduler.who_has)
            assert x.cancelled()
            assert len(c.nthreads()) == 2

            with pytest.raises(CancelledError):
                x.result()

            y = c.submit(div, 1, 3)
            assert y.result() == 1 / 3


@gen_cluster(Worker=Nanny, client=True, timeout=60)
async def test_restart_fast(c, s, a, b):
    L = c.map(sleep, range(10))

    start = time()
    await c.restart()
    assert time() - start < 10
    assert len(s.workers) == 2

    assert all(x.status == "cancelled" for x in L)

    x = c.submit(inc, 1)
    result = await x
    assert result == 2


def test_worker_doesnt_await_task_completion(loop):
    with cluster(nanny=True, nworkers=1) as (s, [w]):
        with Client(s["address"], loop=loop) as c:
            future = c.submit(sleep, 100)
            sleep(0.1)
            start = time()
            c.restart()
            stop = time()
            assert stop - start < 20


def test_restart_fast_sync(loop):
    with cluster(nanny=True) as (s, [a, b]):
        with Client(s["address"], loop=loop) as c:
            L = c.map(sleep, range(10))

            start = time()
            c.restart()
            assert time() - start < 10
            assert len(c.nthreads()) == 2

            assert all(x.status == "cancelled" for x in L)

            x = c.submit(inc, 1)
            assert x.result() == 2


@gen_cluster(Worker=Nanny, client=True, timeout=60)
async def test_fast_kill(c, s, a, b):
    L = c.map(sleep, range(10))

    start = time()
    await c.restart()
    assert time() - start < 10

    assert all(x.status == "cancelled" for x in L)

    x = c.submit(inc, 1)
    result = await x
    assert result == 2


@gen_cluster(Worker=Nanny, timeout=60)
async def test_multiple_clients_restart(s, a, b):
    c1 = await Client(s.address, asynchronous=True)
    c2 = await Client(s.address, asynchronous=True)

    x = c1.submit(inc, 1)
    y = c2.submit(inc, 2)
    xx = await x
    yy = await y
    assert xx == 2
    assert yy == 3

    await c1.restart()

    assert x.cancelled()
    start = time()
    while not y.cancelled():
        await asyncio.sleep(0.01)
        assert time() < start + 5

    await c1.close()
    await c2.close()


@gen_cluster(Worker=Nanny, timeout=60)
async def test_restart_scheduler(s, a, b):
    assert len(s.workers) == 2
    pids = (a.pid, b.pid)
    assert pids[0]
    assert pids[1]

    await s.restart()

    assert len(s.workers) == 2
    pids2 = (a.pid, b.pid)
    assert pids2[0]
    assert pids2[1]
    assert pids != pids2


@gen_cluster(Worker=Nanny, client=True, timeout=60)
async def test_forgotten_futures_dont_clean_up_new_futures(c, s, a, b):
    x = c.submit(inc, 1)
    await c.restart()
    y = c.submit(inc, 1)
    del x

    # Ensure that the profiler has stopped and released all references to x so that it can be garbage-collected
    with profile.lock:
        pass
    await asyncio.sleep(0.1)
    await y


@pytest.mark.slow
@pytest.mark.flaky(condition=MACOS, reruns=10, reruns_delay=5)
@gen_cluster(client=True, timeout=60, active_rpc_timeout=10)
async def test_broken_worker_during_computation(c, s, a, b):
    s.allowed_failures = 100
    async with Nanny(s.address, nthreads=2) as n:
        start = time()
        while len(s.workers) < 3:
            await asyncio.sleep(0.01)
            assert time() < start + 5

        N = 256
        expected_result = N * (N + 1) // 2
        i = 0
        L = c.map(inc, range(N), key=["inc-%d-%d" % (i, j) for j in range(N)])
        while len(L) > 1:
            i += 1
            L = c.map(
                slowadd,
                *zip(*partition_all(2, L)),
                key=["add-%d-%d" % (i, j) for j in range(len(L) // 2)],
            )

        await asyncio.sleep(random.random() / 20)
        with suppress(CommClosedError):  # comm will be closed abrupty
            await c.run(os._exit, 1, workers=[n.worker_address])

        await asyncio.sleep(random.random() / 20)
        while len(s.workers) < 3:
            await asyncio.sleep(0.01)

        with suppress(
            CommClosedError, EnvironmentError
        ):  # perhaps new worker can't be contacted yet
            await c.run(os._exit, 1, workers=[n.worker_address])

        [result] = await c.gather(L)
        assert isinstance(result, int)
        assert result == expected_result


@gen_cluster(client=True, Worker=Nanny, timeout=60)
async def test_restart_during_computation(c, s, a, b):
    xs = [delayed(slowinc)(i, delay=0.01) for i in range(50)]
    ys = [delayed(slowinc)(i, delay=0.01) for i in xs]
    zs = [delayed(slowadd)(x, y, delay=0.01) for x, y in zip(xs, ys)]
    total = delayed(sum)(zs)
    result = c.compute(total)

    await asyncio.sleep(0.5)
    assert any(ws.processing for ws in s.workers.values())
    await c.restart()
    assert not any(ws.processing for ws in s.workers.values())

    assert not s.tasks


class SlowTransmitData:
    def __init__(self, data, delay=0.1):
        self.delay = delay
        self.data = data

    def __reduce__(self):
        import time

        time.sleep(self.delay)
        return (SlowTransmitData, (self.delay,))

    def __sizeof__(self) -> int:
        # Ensure this is offloaded to avoid blocking loop
        import dask
        from dask.utils import parse_bytes

        return parse_bytes(dask.config.get("distributed.comm.offload")) + 1


@pytest.mark.slow
@gen_cluster(client=True)
async def test_worker_who_has_clears_after_failed_connection(c, s, a, b):
    """This test is very sensitive to cluster state consistency. Timeouts often
    indicate subtle deadlocks. Be mindful when marking flaky/repeat/etc."""
    async with Nanny(s.address, nthreads=2) as n:
        while len(s.workers) < 3:
            await asyncio.sleep(0.01)

        def slow_ser(x, delay):
            return SlowTransmitData(x, delay=delay)

        n_worker_address = n.worker_address
        futures = c.map(
            slow_ser,
            range(20),
            delay=0.1,
            key=["f%d" % i for i in range(20)],
            workers=[n_worker_address],
            allow_other_workers=True,
        )

        def sink(*args):
            pass

        await wait(futures)
        result_fut = c.submit(sink, futures, workers=a.address)

        with suppress(CommClosedError):
            await c.run(os._exit, 1, workers=[n_worker_address])

        while len(s.workers) > 2:
            await asyncio.sleep(0.01)

        await result_fut

        assert not a.state.has_what.get(n_worker_address)
        assert not any(
            n_worker_address in s for ts in a.state.tasks.values() for s in ts.who_has
        )


@gen_cluster(
    client=True,
    nthreads=[("127.0.0.1", 1), ("127.0.0.1", 2), ("127.0.0.1", 3)],
)
async def test_worker_same_host_replicas_missing(c, s, a, b, x):
    # See GH4784
    def mock_address_host(addr):
        # act as if A and X are on the same host
        nonlocal a, b, x
        if addr in [a.address, x.address]:
            return "A"
        else:
            return "B"

    with mock.patch("distributed.worker.get_address_host", mock_address_host):
        futures = c.map(
            slowinc,
            range(20),
            delay=0.1,
            key=["f%d" % i for i in range(20)],
            workers=[a.address],
            allow_other_workers=True,
        )
        await wait(futures)

        # replicate data to avoid the scheduler retriggering the computation
        # retriggering cleans up the state nicely but doesn't reflect real world
        # scenarios where there may be replicas on the cluster, e.g. they are
        # replicated as a dependency somewhere else
        await c.replicate(futures, n=2, workers=[a.address, b.address])

        def sink(*args):
            pass

        # Since A and X are mocked to be co-located, X will consistently pick A
        # to fetch data from. It will never succeed since we're removing data
        # artificially, without notifying the scheduler.
        # This can only succeed if B handles the missing data properly by
        # removing A from the known sources of keys
        a.handle_stimulus(
            FreeKeysEvent(keys=["f1"], stimulus_id="Am I evil?")
        )  # Yes, I am!
        result_fut = c.submit(sink, futures, workers=x.address)

        await result_fut


@pytest.mark.slow
@gen_cluster(client=True, timeout=60, Worker=Nanny, nthreads=[("127.0.0.1", 1)])
async def test_restart_timeout_on_long_running_task(c, s, a):
    with captured_logger("distributed.scheduler") as sio:
        future = c.submit(sleep, 3600)
        await asyncio.sleep(0.1)
        await c.restart()

    text = sio.getvalue()
    assert "timeout" not in text.lower()


@pytest.mark.slow
@gen_cluster(client=True, scheduler_kwargs={"worker_ttl": "500ms"})
async def test_worker_time_to_live(c, s, a, b):
    from distributed.scheduler import heartbeat_interval

    assert set(s.workers) == {a.address, b.address}

    a.periodic_callbacks["heartbeat"].stop()
    while a.heartbeat_active:
        await asyncio.sleep(0.01)

    start = time()
    while set(s.workers) == {a.address, b.address}:
        await asyncio.sleep(0.01)
    assert set(s.workers) == {b.address}

    # Worker removal is triggered after 10 * heartbeat
    # This is 10 * 0.5s at the moment of writing.
    interval = 10 * heartbeat_interval(len(s.workers))
    # Currently observing an extra 0.3~0.6s on top of the interval.
    # Adding some padding to prevent flakiness.
    assert time() - start < interval + 2.0


@gen_cluster(client=True, nthreads=[("", 1)])
async def test_forget_data_not_supposed_to_have(c, s, a):
    """If a dependency fetch finishes on a worker after the scheduler already released
    everything, the worker might be stuck with a redundant replica which is never
    cleaned up.
    """
    async with BlockedGatherDep(s.address) as b:
        x = c.submit(inc, 1, key="x", workers=[a.address])
        y = c.submit(inc, x, key="y", workers=[b.address])

        await b.in_gather_dep.wait()
        assert b.state.tasks["x"].state == "flight"

        x.release()
        y.release()
        while s.tasks:
            await asyncio.sleep(0.01)

        b.block_gather_dep.set()
        while b.state.tasks:
            await asyncio.sleep(0.01)


@gen_cluster(
    client=True,
    nthreads=[("127.0.0.1", 1) for _ in range(3)],
    config={"distributed.comm.timeouts.connect": "1s"},
    Worker=Nanny,
)
async def test_failing_worker_with_additional_replicas_on_cluster(c, s, *workers):
    """
    If a worker detects a missing dependency, the scheduler is notified. If no
    other replica is available, the dependency is rescheduled. A reschedule
    typically causes a lot of state to be reset. However, if another replica is
    available, we'll need to ensure that the worker can detect outdated state
    and correct its state.
    """

    def slow_transfer(x, delay=0.1):
        return SlowTransmitData(x, delay=delay)

    def dummy(*args, **kwargs):
        return

    import psutil

    proc = psutil.Process(workers[1].pid)
    f1 = c.submit(
        slow_transfer,
        1,
        key="f1",
        workers=[workers[0].worker_address],
    )
    # We'll schedule tasks on two workers, s.t. f1 is replicated. We will
    # suspend one of the workers and kill the origin worker of f1 such that a
    # comm failure causes the worker to handle a missing dependency. It will ask
    # the schedule such that it knows that a replica is available on f2 and
    # reschedules the fetch
    f2 = c.submit(dummy, f1, pure=False, key="f2", workers=[workers[1].worker_address])
    f3 = c.submit(dummy, f1, pure=False, key="f3", workers=[workers[2].worker_address])

    await wait(f1)
    proc.suspend()

    await wait(f3)
    await workers[0].close()

    proc.resume()
    await c.gather([f1, f2, f3])
