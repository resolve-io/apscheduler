from threading import Thread, Event, Lock
from datetime import datetime, timedelta
from logging import getLogger

from apscheduler.util import time_difference
from apscheduler.triggers import *


logger = getLogger(__name__)


class Job(object):
    """
    Represents a tasks scheduled in the scheduler.
    """

    def __init__(self, trigger, func, args, kwargs):
        self.thread = None
        self.trigger = trigger
        self.func = func
        self.args = args
        self.kwargs = kwargs
        self.error_callbacks = []
        if hasattr(func, '__name__'):
            self.name = func.__name__
        else:
            self.name = str(func)
    
    def run(self):
        """
        Starts the execution of this job in a separate thread.
        """
        if (self.thread and self.thread.isAlive()):
            logger.info('Skipping run of job %s (previously triggered '
                        'instance is still running)', self)
        else:
            self.thread = Thread(target=self.run_in_thread)
            self.thread.start()
    
    def run_in_thread(self):
        """
        Runs the associated callable.
        This method is executed in a dedicated thread.
        """
        try:
            self.func(*self.args, **self.kwargs)
        except:
            logger.exception('Error executing job "%s"', self)
            raise

    def __str__(self):
        return self.name


class JobHandle(object):
    def __init__(self, job, scheduler):
        self.job = job
        self.scheduler = scheduler

    def unschedule(self):
        """
        Removes the associated job from the scheduler's job list,
        so it won't be executed again.
        """
        self.scheduler.unschedule_job(self.job)
    
    def is_active(self):
        """
        Determines if the associated job is still on the job list
        of the associated scheduler (if it still exists).
        
        @return: True if the associated job is still active, False if not
        """
        self.scheduler.jobs_lock.acquire()
        try:
            return self.job in self.scheduler.jobs
        finally:
            self.scheduler.jobs_lock.release()
    
    def __str__(self):
        return str(self.job)


class SchedulerShutdownError(Exception):
    """
    Thrown when attempting to use the scheduler after
    it's been shut down.
    """
    def __init__(self):
        Exception.__init__(self, 'Scheduler has already been shut down')


class SchedulerAlreadyRunningError(Exception):
    """
    Thrown when attempting to start the scheduler, but it's already running.
    """
    def __init__(self):
        Exception.__init__(self, 'Scheduler is already running')


class Scheduler(object):
    stopped = False
    thread = None
    misfire_grace_time = 1

    def __init__(self, **config):
        self.jobs = []
        self.jobs_lock = Lock()
        self.wakeup = Event()
        self.configure(config)
    
    def configure(self, config):
        """
        Updates the configuration with the given options.
        """
        for key, val in config.items():
            if key == 'misfire_grace_time':
                self.misfire_grace_time = int(val)
    
    def start(self):
        """
        Starts the scheduler in a new thread.
        """
        if self.thread and self.thread.isAlive():
            raise SchedulerAlreadyRunningError
        self.thread = Thread(target=self._run, name='APScheduler')
        self.thread.start()
    
    def shutdown(self, timeout=None):
        """
        Shuts down the scheduler and terminates the thread.
        Does not terminate any currently running jobs.
        
        @param timeout: time (in seconds) to wait for the scheduler thread to
            terminate, or None to skip waiting
        """
        if self.stopped:
            raise SchedulerShutdownError
        self.stopped = True
        self.wakeup.set()
        if timeout:
            self.thread.join(timeout)
        self.jobs = []

    def cron_schedule(self, year='*', month='*', day='*', day_of_week='*',
                      hour='*', minute='*', second='*', args=None,
                      kwargs=None):
        """
        Decorator that causes its host function to be scheduled
        according to the given parameters.
        This decorator does not wrap its host function.
        The scheduled function will be called without any arguments.
        @see: add_cron_job
        """
        def inner(func):
            self.add_cron_job(func, year, month, day, day_of_week, hour,
                              minute, second, args, kwargs)
            return func
        return inner

    def interval_schedule(self, weeks=0, days=0, hours=0, minutes=0, seconds=0,
                          start_date=None, repeat=0, args=None, kwargs=None):
        """
        Decorator that causes its host function to be scheduled
        for execution on specified intervals.
        This decorator does not wrap its host function.
        The scheduled function will be called without any arguments.
        Note that the default repeat value is 0, which means to repeat forever.
        @see: add_delayed_job
        """
        def inner(func):
            self.add_interval_job(func, weeks, days, hours, minutes, seconds,
                                  start_date, repeat, args, kwargs)
            return func
        return inner
    
    def add_job(self, func, date, args=None, kwargs=None):
        """
        Adds a job to be completed on a specific date and time.

        @param func: callable to run
        @param args: positional arguments to call func with
        @param kwargs: keyword arguments to call func with
        """
        trigger = DateTrigger(date)
        return self._add_job(trigger, func, args, kwargs)
    
    def add_interval_job(self, func, weeks=0, days=0, hours=0, minutes=0,
                         seconds=0, start_date=None, repeat=1, args=None,
                         kwargs=None):
        """
        Adds a job to be completed on specified intervals.

        @param func: callable to run
        @param weeks: number of weeks to wait
        @param days: number of days to wait
        @param hours: number of hours to wait
        @param minutes: number of minutes to wait
        @param seconds: number of seconds to wait
        @param start_date: when to first execute the job and start the
            counter (default is after the given interval)
        @param repeat: number of times the job will be run (0 = repeat
            indefinitely)
        @param args: list of positional arguments to call func with
        @param kwargs: dict of keyword arguments to call func with
        """
        interval = timedelta(weeks=weeks, days=days, hours=hours,
                             minutes=minutes, seconds=seconds)
        trigger = IntervalTrigger(interval, repeat, start_date)
        return self._add_job(trigger, func, args, kwargs)

    def add_cron_job(self, func, year='*', month='*', day='*', day_of_week='*',
                     hour='*', minute='*', second='*', args=None, kwargs=None):
        """
        Adds a job to be completed on specified intervals.
        
        The possible syntaxes for calendar fields are:
        '*' (fire on every value)
        '*/a' (fire every a)
        'a' (fire on the specified value)
        a (same as the previous, but given directly as an integer)
        'a-b' (range; a must be smaller than b)
        'a-b/c' stepped range field, fires every c within the a-b range
        'last' (last valid value, only useful for the month field)
        'x,y,z,...' (fire on any matching expression; can combine any of the
        above)

        @param func: callable to run
        @param year: year to run on
        @param month: month to run on (0 = January)
        @param day: day of month to run on
        @param day_of_week: weekday to run on (0 = Monday)
        @param hour: hour to run on
        @param second: second to run on
        @param args: list of positional arguments to call func with
        @param kwargs: dict of keyword arguments to call func with
        @return: a handle to the scheduled job
        @rtype: JobHandle
        """
        trigger = CronTrigger(year, month, day, day_of_week, hour, minute,
                              second)
        return self._add_job(trigger, func, args, kwargs)

    def unschedule_job(self, job):
        """
        Removes a job, preventing it from being fired any more.
        """
        self.jobs_lock.acquire()
        try:
            self.jobs.remove(job)
        finally:
            self.jobs_lock.release()
        logger.info('Removed job "%s"', job)
        self.wakeup.set()

    def unschedule_func(self, func):
        """
        Removes all jobs that would execute the given function.
        """
        self.jobs_lock.acquire()
        try:
            remove_list = [job for job in self.jobs if job.func is func]
            for job in remove_list:
                self.jobs.remove(job)
                logger.info('Removed job "%s"', job)
        finally:
            self.jobs_lock.release()
        
        # Have the scheduler calculate a new wakeup time
        self.wakeup.set()
    
    def _add_job(self, trigger, func, args, kwargs):
        """
        Adds a Job to the job list and notifies the scheduler thread.

        @param trigger: trigger for the given callable
        @param args: list of positional arguments to call func with
        @param kwargs: dict of keyword arguments to call func with
        @return: a handle to the scheduled job
        @rtype: JobHandle
        """
        if self.stopped:
            raise SchedulerShutdownError
        if not hasattr(func, '__call__'):
            raise TypeError('func must be callable')

        if args is None:
            args = []
        if kwargs is None:
            kwargs = {}

        job = Job(trigger, func, args, kwargs)
        self.jobs_lock.acquire()
        try:
            self.jobs.append(job)
        finally:
            self.jobs_lock.release()
        logger.info('Added job "%s"', job)
       
        # Notify the scheduler about the new job
        self.wakeup.set()

        return JobHandle(job, self)

    def _get_next_wakeup_time(self, now):
        """
        Determines the time of the next job execution, and removes finished
        jobs.

        @param now: the result of datetime.now(), generated elsewhere for
            consistency.
        """
        next_wakeup = None
        finished_jobs = []

        self.jobs_lock.acquire()
        try:
            for job in self.jobs:
                next_run = job.trigger.get_next_fire_time(now)
                if next_run is None:
                    finished_jobs.append(job)
                elif next_run and (next_wakeup is None or \
                                   next_run < next_wakeup):
                    next_wakeup = next_run

            # Clear out any finished jobs
            for job in finished_jobs:
                self.jobs.remove(job)
                logger.info('Removed finished job "%s"', job)
        finally:
            self.jobs_lock.release()

        return next_wakeup
    
    def _get_current_jobs(self):
        """
        Determines which jobs should be executed right now.
        """
        current_jobs = []
        now = datetime.now()
        start = now - timedelta(seconds=self.misfire_grace_time)
        
        self.jobs_lock.acquire()
        try:
            for job in self.jobs:
                next_run = job.trigger.get_next_fire_time(start)
                if next_run:
                    time_diff = time_difference(now, next_run)
                    if next_run < now and time_diff <= self.misfire_grace_time:
                        current_jobs.append(job)
        finally:
            self.jobs_lock.release()

        return current_jobs
    
    def _run(self):
        """
        Runs the main loop of the scheduler.
        """
        self.wakeup.clear()
        while not self.stopped:
            # Execute any jobs scheduled to be run right now
            for job in self._get_current_jobs():
                logger.debug('Executing job "%s"', job)
                job.run()

            # Figure out when the next job should be run, and
            # adjust the wait time accordingly
            now = datetime.now()
            next_wakeup_time = self._get_next_wakeup_time(now)

            # Sleep until the next job is scheduled to be run,
            # or a new job is added, or the scheduler is stopped
            if next_wakeup_time is not None:
                wait_seconds = time_difference(next_wakeup_time, now)
                logger.debug('Next wakeup is due at %s (in %f seconds)',
                             next_wakeup_time, wait_seconds)
                self.wakeup.wait(wait_seconds)
            else:
                logger.debug('No jobs; waiting until a job is added')
                self.wakeup.wait()
            self.wakeup.clear()
            
           