#!/usr/bin/env python
from sys import argv
from nnodes import root


def bin():
    root.init()

    if len(argv) > 1:
        root.job.create(argv[1])

    else:
        root.job.create()
