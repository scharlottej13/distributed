from __future__ import annotations

import random
from concurrent.futures import (
    FIRST_COMPLETED,
    FIRST_EXCEPTION,
    Future,
    TimeoutError,
    as_completed,
    wait,
)
from time import sleep

import pytest
from tlz import take

from distributed.metrics import time
from distributed.utils import CancelledError
from distributed.utils_test import inc, slowadd, slowdec, slowinc, throws, varying


def number_of_processing_tasks(client):
    return sum(len(v) for k, v in client.processing().items())


def test_submit(client):
    with client.get_executor() as e:
        f1 = e.submit(slowadd, 1, 2)
        assert isinstance(f1, Future)
        f2 = e.submit(slowadd, 3, y=4)
        f3 = e.submit(throws, "foo")
        f4 = e.submit(slowadd, x=5, y=6)
        assert f1.result() == 3
        assert f2.result() == 7
        with pytest.raises(RuntimeError):
            f3.result()
        assert f4.result() == 11


def test_as_completed(client):
    with client.get_executor() as e:
        N = 10
        fs = [e.submit(slowinc, i, delay=0.02) for i in range(N)]
        expected = set(range(1, N + 1))

        for f in as_completed(fs):
            res = f.result()
            assert res in expected
            expected.remove(res)

        assert not expected


def test_wait(client):
    with client.get_executor(pure=False) as e:
        N = 10
        fs = [e.submit(slowinc, i, delay=0.05) for i in range(N)]
        res = wait(fs, timeout=0.01)
        assert len(res.not_done) > 0
        res = wait(fs)
        assert len(res.not_done) == 0
        assert res.done == set(fs)

        fs = [e.submit(slowinc, i, delay=0.05) for i in range(N)]
        res = wait(fs, return_when=FIRST_COMPLETED)
        assert len(res.not_done) > 0
        assert len(res.done) >= 1
        res = wait(fs)
        assert len(res.not_done) == 0
        assert res.done == set(fs)

        fs = [e.submit(slowinc, i, delay=0.05) for i in range(N)]
        fs += [e.submit(throws, None)]
        fs += [e.submit(slowdec, i, delay=0.05) for i in range(N)]
        res = wait(fs, return_when=FIRST_EXCEPTION)
        assert any(f.exception() for f in res.done)
        assert res.not_done

        errors = []
        for fs in res.done:
            try:
                fs.result()
            except RuntimeError as e:
                errors.append(e)

        assert len(errors) == 1
        assert "hello" in str(errors[0])


def test_cancellation(client):
    with client.get_executor(pure=False) as e:
        fut = e.submit(sleep, 2.0)
        start = time()
        while number_of_processing_tasks(client) == 0:
            assert time() < start + 30
            sleep(0.01)
        assert not fut.done()

        fut.cancel()
        assert fut.cancelled()
        start = time()
        while number_of_processing_tasks(client) != 0:
            assert time() < start + 30
            sleep(0.01)

        with pytest.raises(CancelledError):
            fut.result()


def test_cancellation_wait(client):
    with client.get_executor(pure=False) as e:
        fs = [e.submit(slowinc, i, delay=0.2) for i in range(10)]
        fs[3].cancel()
        res = wait(fs, return_when=FIRST_COMPLETED, timeout=30)
        assert len(res.not_done) > 0
        assert len(res.done) >= 1

        assert fs[3] in res.done
        assert fs[3].cancelled()


def test_cancellation_as_completed(client):
    with client.get_executor(pure=False) as e:
        fs = [e.submit(slowinc, i, delay=0.2) for i in range(10)]
        fs[3].cancel()
        fs[8].cancel()

        n_cancelled = sum(f.cancelled() for f in as_completed(fs, timeout=30))
        assert n_cancelled == 2


@pytest.mark.slow()
def test_map(client):
    with client.get_executor() as e:
        N = 10
        it = e.map(inc, range(N))
        expected = set(range(1, N + 1))
        for x in it:
            expected.remove(x)
        assert not expected

    with client.get_executor(pure=False) as e:
        N = 10
        it = e.map(slowinc, range(N), [0.3] * N, timeout=1.2)
        results = []
        with pytest.raises(TimeoutError):
            for x in it:
                results.append(x)
        assert 2 <= len(results) < 7

    with client.get_executor(pure=False) as e:
        N = 10
        # Not consuming the iterator will cancel remaining tasks
        it = e.map(slowinc, range(N), [0.3] * N)
        for x in take(2, it):
            pass
        # Some tasks still processing
        assert number_of_processing_tasks(client) > 0
        # Garbage collect the iterator => remaining tasks are cancelled
        del it
        sleep(0.5)
        assert number_of_processing_tasks(client) == 0


def get_random():
    return random.random()


def test_pure(client):
    N = 10
    with client.get_executor() as e:
        fs = [e.submit(get_random) for i in range(N)]
        res = [fut.result() for fut in as_completed(fs)]
        assert len(set(res)) < len(res)
    with client.get_executor(pure=False) as e:
        fs = [e.submit(get_random) for i in range(N)]
        res = [fut.result() for fut in as_completed(fs)]
        assert len(set(res)) == len(res)


def test_workers(client, s, a, b):
    N = 10
    with client.get_executor(workers=[b["address"]]) as e:
        fs = [e.submit(slowinc, i) for i in range(N)]
        wait(fs)
        has_what = client.has_what()
        assert not has_what.get(a["address"])
        assert len(has_what[b["address"]]) == N


def test_unsupported_arguments(client, s, a, b):
    with pytest.raises(TypeError) as excinfo:
        client.get_executor(workers=[b["address"]], foo=1, bar=2)
    assert "unsupported arguments to ClientExecutor: ['bar', 'foo']" in str(
        excinfo.value
    )


def test_retries(client):
    args = [ZeroDivisionError("one"), ZeroDivisionError("two"), 42]

    with client.get_executor(retries=5, pure=False) as e:
        future = e.submit(varying(args))
        assert future.result() == 42

    with client.get_executor(retries=4) as e:
        future = e.submit(varying(args))
        result = future.result()
        assert result == 42

    with client.get_executor(retries=2) as e:
        future = e.submit(varying(args))
        with pytest.raises(ZeroDivisionError, match="two"):
            res = future.result()

    with client.get_executor(retries=0) as e:
        future = e.submit(varying(args))
        with pytest.raises(ZeroDivisionError, match="one"):
            res = future.result()


def test_shutdown_wait(client):
    # shutdown(wait=True) waits for pending tasks to finish
    e = client.get_executor()
    start = time()
    fut = e.submit(sleep, 1.0)
    e.shutdown()
    assert time() >= start + 1.0
    sleep(0.1)  # wait for future outcome to propagate
    assert fut.done()
    fut.result()  # doesn't raise

    with pytest.raises(RuntimeError):
        e.submit(sleep, 1.0)


def test_shutdown_nowait(client):
    # shutdown(wait=False) cancels pending tasks
    e = client.get_executor()
    start = time()
    fut = e.submit(sleep, 5.0)
    e.shutdown(wait=False)
    assert time() < start + 2.0
    sleep(0.1)  # wait for future outcome to propagate
    assert fut.cancelled()

    with pytest.raises(RuntimeError):
        e.submit(sleep, 1.0)
