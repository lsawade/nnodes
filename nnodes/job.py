import typing as tp
import math
from time import time
from os import path, environ
from subprocess import check_call


class Job:
    """Base class for clusters."""
    # job name
    name: str | None = None

    # number of nodes to request
    nnodes: int

    # account to submit the job
    account: str | None = None

    # amount of walltime to request
    walltime: float

    # submit to debug queue and do not requeue if job fails
    debug: bool = False

    # resubmit if job fails
    auto_requeue: bool = True

    # no walltime and nnodes options
    no_scheduler = False

    # avoid calling new MPI tasks if remaining walltime is less than certain minutes
    gap: float = 0.0

    # arbitary arguments added after mpiexec
    exec_args: str | None = None

    # number of CPUs per node (if is None, the value must exist in config.toml)
    cpus_per_node: int

    # number of GPUs per node (if is None, the value must exist in config.toml)
    gpus_per_node: int

    # whether a node can run multiple MPI tasks
    node_splittable = False

    # use multiprocessing instead of MPI
    use_multiprocessing = False

    # maximum number of processes spawned with multiprocessing
    mp_nprocs_max: int = 20

    # execution start time
    _exec_start: float

    # job is being requeued
    _signaled = False

    # job state
    _state: tp.List[bool]

    @property
    def paused(self):
        """Job paused due to insuffcient time."""
        return self._state[0]

    @paused.setter
    def paused(self, key: bool):
        self._state[0] = key

    @property
    def failed(self):
        """Any task failed during execution."""
        return self._state[1]

    @failed.setter
    def failed(self, key: bool):
        self._state[1] = key

    @property
    def aborted(self):
        """Any task failed twice during execution."""
        return self._state[2]

    @aborted.setter
    def aborted(self, key: bool):
        self._state[2] = key

    @property
    def inqueue(self) -> bool:
        """Job is allocated from scheduler (enables automatic requeue and mpiexec timeout)."""
        return False

    @property
    def remaining(self) -> float:
        """Remaining walltime in minutes."""
        return self.walltime - self.gap - (time() - self._exec_start) / 60

    def write(self, cmd: str, dst: str):
        """Write job submission script to target directory."""
        from  .root import root

        root.write(cmd, path.join(dst, 'job.sh'))

    def requeue(self):
        """Resubmit current job."""

    def mpiexec(self, cmd: str, nprocs: int, cpus_per_proc: int = 1, gpus_per_proc: int = 0,
        mps: int | None = None, args: str | None = None) -> str:
        """Returns the command to run an MPI task."""
        raise NotImplementedError(f'mpiexec is not implemented ({cmd})')

    def __init__(self, job: dict, state: list):
        # job state (paused, failed, aborted)
        self._state = state

        # set job attributes
        required_keys = ['nnodes', 'walltime', 'cpus_per_node', 'gpus_per_node']

        for key in required_keys:
            if key not in job and not hasattr(self, key):
                raise KeyError(f'required job config `{key}` is missing')

        for key, val in job.items():
            setattr(self, key, val)

        # execution start time
        self._exec_start = time()

    def create(self, dst: str | None = None):
        """Creates a directory as job workspace."""
        from .root import root

        if dst is None:
            # write job script in current directory
            if root.has('job.bash'):
                raise FileExistsError(f'job.bash already exists')

            dst = '.'

        else:
            # write job script in a subdirectory
            if root.has(dst):
                raise FileExistsError(f'{dst} already exists')

        # copy config.toml
        root.dump(root.load('config.toml'), path.join(dst, 'config.toml'))

        # write job submission script
        self.write('python -c "from nnodes import root; root.run()"', dst)


class LSF(Job):
    """LSF-based cluster."""
    nnmk_name = 'LSF'

    @property
    def inqueue(self):
        return bool(environ.get('LSB_JOBID')) and environ.get('LSB_INTERACTIVE') != 'Y'

    def write(self, cmd, dst):
        from .root import root

        # hours and minutes
        hh = int(self.walltime // 60)
        mm = int(self.walltime - hh * 60)

        # job name
        if self.name:
            if dst == '.':
                name = self.name

            else:
                name = f'{self.name}_{dst}'

        else:
            name = dst

        # job script
        lines = [
            '#!/bin/bash',
            f'#BSUB -J {name}',
            f'#BSUB -W {hh:02d}:{mm:02d}',
            f'#BSUB -nnodes {self.nnodes}',
            f'#BSUB -o lsf.%J.o',
            f'#BSUB -e lsf.%J.e',
            f'#BSUB -alloc_flags "gpumps"'
        ]

        if self.account:
            lines.append(f'#BSUB -P {self.account}')

        if self.debug:
            lines.append('#BSUB -q debug')

        # add main command
        lines.append(cmd + '\n')

        # write to workspace
        root.writelines(lines, path.join(dst, 'job.bash'))

    def requeue(self):
        """Run current job again."""
        check_call('brequeue ' + environ['LSB_JOBID'], shell=True)

    def mpiexec(self, cmd: str, nprocs: int, cpus_per_proc: int = 1, gpus_per_proc: int = 0,
        mps: int | None = None, args: str | None = None):
        """Get the command to call MPI."""
        cmds = ['jsrun']

        if nprocs == 1:
            # avoid MPI warning in Summit
            cmds.append('--smpiargs="off"')

        # Number of MPI tasks (ranks) per resource set
        a = 1

        if mps is not None:
            # <mps> tasks per resource set
            a = mps

            # <mps> CPUs per resource set
            cpus_per_proc *= mps

            # 1 GPU per resource set
            gpus_per_proc = 1

            # number of resource sets
            nprocs //= mps

        cmds.append(f'-n {nprocs} -a {a} -c {cpus_per_proc} -g {gpus_per_proc}')

        if args is not None:
            cmds.append(args)

        cmds.append(cmd)

        return ' '.join(cmds)


class Summit(LSF):
    nnmk_name = 'Oak Ridge Summit (LSF)'

    # number of CPUs per node
    cpus_per_node = 42

    # number of GPUs per node
    gpus_per_node = 6


class Slurm(Job):
    """Slurm-based cluster."""
    nnmk_name = 'Slurm'

    @property
    def inqueue(self):
        return bool(environ.get('SLURM_JOB_ID'))

    def requeue(self):
        """Run current job again."""
        check_call('scontrol requeue ' + environ['SLURM_JOB_ID'], shell=True)

    def mpiexec(self, cmd: str, nprocs: int, cpus_per_proc: int = 1, gpus_per_proc: int = 0,
        mps: int | None = None, args: str | None = None):
        """Get the command to call MPI."""
        cmds = [f'srun -n {nprocs} --cpus-per-task  {cpus_per_proc} --gpus-per-task {gpus_per_proc}']

        if args is not None:
            cmds.append(args)

        if args is None or '--ntasks-per-core' not in args:
            cmds.append('--ntasks-per-core 1')

        cmds.append(cmd)

        return ' '.join(cmds)

    def write(self, cmd, dst):
        from .root import root

        # hours and minutes
        hh = int(self.walltime // 60)
        mm = int(self.walltime - hh * 60)

        # job name
        if self.name:
            if dst == '.':
                name = self.name

            else:
                name = f'{self.name}_{dst}'

        else:
            name = dst

        # job script
        lines = [
            '#!/bin/bash',
            f'#BSUB -J {name}',
            f'#BSUB -W {hh:02d}:{mm:02d}',
            f'#BSUB -nnodes {self.nnodes}',
            f'#BSUB -o lsf.%J.o',
            f'#BSUB -e lsf.%J.e',
            f'#BSUB -alloc_flags "gpumps"'
        ]
        lines = [
            '#!/bin/bash',
            f'#SBATCH -J {name}',
            f'#SBATCH -t {hh:02d}:{mm:02d}:00',
            f'#SBATCH -N {self.nnodes}',
            f'#SBATCH -o slurm.%J.o',
            f'#SBATCH -e slurm.%J.e'
        ]

        if self.account:
            lines.append(f'#SBATCH -A {self.account}')

        # add main command
        lines.append(cmd + '\n')

        # write to workspace
        root.writelines(lines, path.join(dst, 'job.bash'))


class Tiger(Slurm):
    """Princeton TigerGPU"""
    nnmk_name = 'Princeton TigerGPU (Slurm)'

    # number of CPUs per node
    cpus_per_node = 28

    # number of GPUs per node
    gpus_per_node = 4


class Andes(Slurm):

    # number of CPUs per node
    cpus_per_node = 32

    # number of GPUs per node
    gpus_per_node = 0

    #task, nprocs, cpus_per_proc, gpus_per_proc, mps, args_cmd
    def mpiexec(self, cmd: str, nprocs: int, cpus_per_proc: int = 1, gpus_per_proc: int = 0,
                mps: int | None = None, args: str | None = None):
        """Get the command to call MPI."""
        # mps, gpus_per_proc are unused
        return  f'srun -n {nprocs} -c {cpus_per_proc} --cpu-bind=cores {args} {cmd}'


class Traverse(Slurm):
    """Princeton Traverse overloaded with gpu-mps flag."""

    nnmk_name = 'Princeton Traverse (Slurm)'

    # number of CPUs per node
    cpus_per_node = 32

    # number of GPUs per node
    gpus_per_node = 4

    def mpiexec(self, cmd: str, nprocs: int, cpus_per_proc: int = 1,
                gpus_per_proc: int = 0, mps: int | None = None):
        """Get the command to call MPI."""

        if mps is not None:
            nodes = max(math.ceil(nprocs/self.cpus_per_node), math.ceil((nprocs//mps)/self.gpus_per_node))
            gpu_opts = f"-N{nodes} --gres=gpu:{self.gpus_per_node} --gpu-mps"
        else:
            gpu_opts = f"--gpus-per-task {gpus_per_proc}"

        return  f'srun -n {nprocs} {gpu_opts} {cmd}'


class DTN(Slurm):
    """Oak Ridge National Lab Data Transfer Node."""
    nnmk_name = 'Oak Ridge DTN (Slurm)'

    # number of CPUs per node
    cpus_per_node = 16

    # number of GPUs per node
    gpus_per_node = 0


class Local(Job):
    """Local computer using multiprocessing instead of MPI."""
    nnmk_name = 'Persional Computer'

    # number of CPUs per node
    cpus_per_node = 1

    # number of GPUs per node
    gpus_per_node = 0

    # no walltime and nnodes options
    no_scheduler = True

    # replace MPI tasks with multiprocessing
    use_multiprocessing = True


class LocalMPI(Local):
    """Local computer with MPI installed."""
    nnmk_name = 'Persional Computer with MPI installed'

    use_multiprocessing = False

    def mpiexec(self, cmd: str, nprocs: int, *_):
        """Get the command to call MPI."""
        return f'$(which mpiexec) -n {nprocs} {cmd}'
