"""Keep pytest out of the perf directory.

The files here are manual benchmarking DRIVERS, not tests. They submit real jobs
to a deployed endpoint when run. Their side effects are already guarded behind
``if __name__ == '__main__'`` and they are named ``perf_driver*`` (not
``test_*`` / ``*_test``) so pytest would not collect them anyway — this
``collect_ignore_glob`` is a belt-and-suspenders guarantee that no file added to
this directory can ever be collected and imported during a test run.
"""

collect_ignore_glob = ["*"]
