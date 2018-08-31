# Local implementation of backend.py using separate tmux sessions for jobs

import os
import shlex
import subprocess
import sys
import time
from typing import Tuple

from . import backend
from . import util

TASKDIR_ROOT = '/tmp/ncluster/task'
SCRATCH_ROOT = '/tmp/ncluster/scratch'
LOGDIR_ROOT = os.environ[
                'HOME'] + '/ncluster/runs'  # use local instead of /tmp because /tmp gets wiped


# TODO: use separate session for each task, for parity with AWS job launcher


# todo: tmux session names are backwards from AWS job names (runname-jobname)
# TODO: add kwargs so that tmux backend can be drop-in replacement


# TODO: rename extra_kwargs to kwargs everywhere
class Task(backend.Task):
  """Local tasks interact with tmux session where session name is derived
  from job name, and window names are task ids."""

  def __init__(self, name, tmux_window, *, install_script='', job=None,
               **kwargs):
    self.tmux_window = tmux_window  # TODO: rename tmux_window to window?
    self.name = name
    self.install_script = install_script
    self.job = job
    self.kwargs = kwargs

    # local servers sometimes listen only on localhost (TensorBoard), and sometimes only on
    # externally assigned ip address from gethostbyname (Ray), use the localhost arbitrarily
    # https://github.com/ray-project/ray/issues/1677
    #    self.public_ip = socket.gethostbyname(socket.gethostname())
    self.public_ip = '127.0.0.1'
    self.ip = self.public_ip

    self.connect_instructions = 'tmux a -t ' + self.tmux_window

    # task current dir
    print('name is', name)
    tmpdir = f"{util.reverse_taskname(name)}.{os.getpid()}.{util.now_micros()}"
    self.taskdir = f"{TASKDIR_ROOT}/{tmpdir}"
    self._scratch = f"{SCRATCH_ROOT}/{tmpdir}"

    self._log(f"Creating taskdir {self.taskdir}")
    self._run_raw('mkdir -p ' + self.taskdir)

    self._log(f"Creating scratch {self._scratch}")
    self._run_raw('rm -Rf ' + self._scratch)
    self._run_raw('mkdir -p ' + self._scratch)
    self._run_counter = 0

    self.run('cd ' + self.taskdir)
    self.install_script = install_script
    for line in install_script.split('\n'):
      self.run(line)

  def run(self, cmd, async=False, ignore_errors=False, **kwargs) -> int:
    self._run_counter += 1
    cmd = cmd.strip()
    if not cmd or cmd.startswith('#'):  # ignore empty/commented out lines
      return -1
    self._log("tmux> %s", cmd)

    cmd_fn = f'{self._scratch}/{self._run_counter}.cmd'
    status_fn = f'{self._scratch}/{self._run_counter}.status'
    assert not os.path.exists(status_fn)

    cmd = util.shell_strip_comment(cmd)
    assert '&' not in cmd, f"cmd {cmd} contains &, that breaks things"

    open(cmd_fn, 'w').write(cmd + '\n')
    modified_cmd = '%s ; echo $? > %s' % (cmd, status_fn)
    modified_cmd = shlex.quote(modified_cmd)

    tmux_cmd = f'tmux send-keys -t {self.tmux_window} {modified_cmd} Enter'
    self._run_raw(tmux_cmd)
    if async:
      return -1

    # TODO: dedup this with file waiting logic in aws_backend
    self._wait_for_file(status_fn)
    contents = open(status_fn).read()

    # if empty wait a bit to allow for race condition
    if len(contents) == 0:
      time.sleep(0.01)
    status = int(open(status_fn).read().strip())

    if status != 0:
      if not ignore_errors:
        raise RuntimeError(f"Command {cmd} returned status {status}")
      else:
        self._log(f"Warning: command {cmd} returned status {status}")

    return status

  # TODO(y): refactor with aws_backend
  def run_with_output(self, cmd, async=False, ignore_errors=False) -> Tuple[str, str]:

    cmd: str = cmd.strip()
    if not cmd or cmd.startswith('#'):  # ignore empty/commented out lines
      return '', ''

    self._log('----%s', cmd)
    assert '\n' not in cmd, "Do not support multi-line commands"

    stdout_fn = f"{self._scratch}/{self._run_counter}.stdout"
    stderr_fn = f"{self._scratch}/{self._run_counter}.stderr"
    cmd2 = f"{cmd} > {stdout_fn} 2> {stderr_fn}"
    status = self.run(cmd2, async, ignore_errors=True)
    stdout = self.file_read(stdout_fn)
    stderr = self.file_read(stderr_fn)

    if status > 0:
      self._log(f"Warning: command '{cmd}' returned {status},"
                f" stdout was '{stdout}' stderr was '{stderr}'")
      if not ignore_errors:
        raise RuntimeError(f"Warning: command '{cmd}' returned {status},"
                           f" stdout was '{stdout}' stderr was '{stderr}'")
    return stdout, stderr

  def _run_raw(self, cmd):
    """Runs command directly, skipping tmux interface"""
    os.system(cmd)

  def get_logdir_root(self):
    return LOGDIR_ROOT

  def upload(self, local_fn, remote_fn=None, dont_overwrite=False):
    """Uploads file to remote instance. If location not specified, dumps it
    into default directory."""

    self._log('uploading ' + local_fn)

    if remote_fn is None:
      remote_fn = os.path.basename(local_fn)
    if dont_overwrite and self.file_exists(remote_fn):
      self._log("Remote file %s exists, skipping" % (remote_fn,))
      return

    # don't allow absolute paths for local backend, things should go into taskdir
    assert not remote_fn.startswith('/')

    remote_fn = self.taskdir+'/'+remote_fn

    local_fn = os.path.abspath(local_fn)
    self.run("cp -R %s %s" % (local_fn, remote_fn))

  def download(self, source_fn, target_fn='.'):
    raise NotImplementedError()
    # self.log("downloading %s to %s"%(source_fn, target_fn))
    # source_fn_full = os.path.abspath(source_fn)
    # os.system("cp %s %s" %(source_fn_full, target_fn))

  def file_exists(self, remote_fn):
    return os.path.exists(remote_fn)

  def file_read(self, remote_fn):
    if self.file_exists(remote_fn):
      return open(remote_fn).read()
    else:
      return ''

  def file_write(self, remote_fn, contents):
    def make_temp_fn():
      """Returns temporary filename for this task."""
      return self._scratch + '/file_write.' + str(util.now_micros())

    tmp_fn = make_temp_fn()
    open(tmp_fn, 'w').write(contents)
    self.upload(tmp_fn, remote_fn)

  def _stream_file(self, fn):
    if not fn.startswith('/'):
      fn = self.taskdir + '/' + fn

    if not os.path.exists(fn):
      os.system('mkdir -p ' + os.path.dirname(fn))
      os.system('touch ' + fn)

    p = subprocess.Popen(['tail', '-f', fn], stdout=subprocess.PIPE)

    for line in iter(p.stdout.readline, ''):
      sys.stdout.write(line.decode('ascii', errors='ignore'))

  def _wait_for_file(self, fn, max_wait_sec=600, check_interval=0.02):
    print("Waiting for file", fn)
    start_time = time.time()
    while True:
      if time.time() - start_time > max_wait_sec:
        assert False, "Timeout %s exceeded for %s" % (max_wait_sec, fn)
      if not self.file_exists(fn):
        time.sleep(check_interval)
        continue
      else:
        break


def make_task(name=None,
              run_name=None,
              **kwargs) -> Task:
  if name is None:
    name = f"{util.now_micros()}"

  # tmux can't use . for session names
  tmux_window = name.replace('.', '-') + ':0'
  tmux_session = tmux_window[:-2]
  util.log(f'killing session {tmux_session}')
  os.system(f'tmux kill-session -t {tmux_session}')
  os.system(f'tmux new-session -s {tmux_session} -n 0 -d')

  dummy_run = backend.Run(run_name)
  dummy_job = dummy_run.make_job()
  task = Task(name, job=dummy_job,
              tmux_window=tmux_window,  # propagate optional args
              **kwargs)
  dummy_job.tasks.append(task)
  return task


def make_job(name=None,
             num_tasks=0,
             run_name=None,
             **kwargs
             ) -> backend.Job:
  assert num_tasks > 0, f"Can't create job with {num_tasks} tasks"

  assert name.count(
    '.') <= 1, "Job name has too many .'s (see ncluster design: Run/Job/Task hierarchy for  convention)"
  tasks = [make_task(f"{i}.{name}") for i in range(num_tasks)]

  dummy_run = backend.Run(run_name)
  job = backend.Job(name, dummy_run, tasks, **kwargs)
  dummy_run.jobs.append(job)
  return job


def make_run(name) -> backend.Run:
  return backend.Run(name)
