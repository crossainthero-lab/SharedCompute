"""Exceptions shared by the agent's workload and device code."""


class Cancelled(Exception):
    """The job was cancelled by the marketplace or exceeded its time limit."""


class Incompatible(Exception):
    """This device cannot run this job (too little free VRAM, CUDA unusable, ...).

    Reported to the marketplace as a graceful rejection: the job is requeued elsewhere without
    costing the customer an attempt.
    """
