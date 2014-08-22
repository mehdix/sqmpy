"""
    sqmpy.job.manager
    ~~~~~

    Manager class along with it's helpers.
"""
import datetime

from flask.ext.login import current_user
from saga.exceptions import BadParameter

from sqmpy import app, db
from sqmpy.core import SQMComponent
from sqmpy.job.exceptions import JobManagerException
from sqmpy.job.helpers import JobFileHandler
from sqmpy.job.models import Job, Resource
from sqmpy.job.constants import JOB_MANAGER, JobStatus, Adaptor
from sqmpy.job.saga_helper import SagaJobWrapper

__author__ = 'Mehdi Sadeghi'


class JobManager(SQMComponent):
    """
    This class is responsible to keep state of the executed jobs.
    """
    def __init__(self):
        super(JobManager, self).__init__(JOB_MANAGER)

        # A dictionary to keep active jobs along with their wrapper objects.
        # Wrapper objects contain the job itself along with related saga objects.
        self.__jobs = {}

    def submit_job(self, job_name, resource_url, uploaded_files, **kwargs):
        """
        Submit a new job along with its input files. Input files will be moved under
            a new folder with this structure: <staging_dir>/<username>/<job_id>/input_files/
        :param name: job name
        :param resource_url: resource to submit job there
        :param uploaded_files: a list of <filename, file_stream, relation> for each given file.
        :param kwargs::
            :param total_cpu_count:
            :param spmd_variation:
            :param walltime_limit:
            :param adaptor: the backend to be used, should be 'shell' or 'sge'
            :param description: about the job
        :return: job id
        """
        # Basic checks
        if not job_name:
            raise JobManagerException("Job name is not defined.")

        if not resource_url:
            raise JobManagerException("Resource is not defined.")

        if not uploaded_files:
            raise JobManagerException('At least script files should be uploaded')

        # Store the job
        job = Job()
        job.name = job_name
        job.submit_date = datetime.datetime.now()
        job.submit_adaptor = kwargs.get('adaptor') or Adaptor.shell.value
        job.last_status = JobStatus.INIT
        job.owner_id = current_user.id
        job.total_cpu_count = kwargs.get('total_cpu_count')
        job.walltime_limit = kwargs.get('walltime_limit')
        job.spmd_variation = kwargs.get('spmd_variation')
        job.description = kwargs.get('description')
        job.remote_dir = kwargs.get('working_directory')

        # Insert a new record for url if it does not exist already
        try:
            resource = Resource.query.filter(Resource.url == resource_url).first()
            if not resource:
                resource = Resource(resource_url, resource_url)
                db.session.add(resource)
                db.session.flush()
            job.resource_id = resource.id
            db.session.add(job)
            db.session.flush()

            # Save staging data before running the job
            # Input files will be moved under a new folder with this structure:
            #   <staging_dir>/<username>/<job_id>/
            # This will also save script file in the mentioned job folder as `job-[JOB_ID]_script'
            # Set to silent because some ghost files are uploaded with no name and empty value, don't know why.
            JobFileHandler.save_input_files(job, uploaded_files, silent=True)

            #TODO: I should find a way to either save state or not to save state when error happens.
            # Create saga wrapper
            saga_wrapper = SagaJobWrapper(job)
            # Keep the created job
            self.__jobs[job.id] = saga_wrapper
            # Run the saga job
            saga_wrapper.run()
        except:
            db.session.rollback()
            raise
        db.session.commit()
        return job.id

    def get_job_status(self, job_id):
        """
        Get job updated status
        :param job_id:
        :return:
        """
        job = self.get_job(job_id)
        if job.id not in self.__jobs:
            if job.last_status not in [JobStatus.CANCELED,
                                       JobStatus.DONE,
                                       JobStatus.FAILED]:
                # If there is not wrapper for the job and the job is not either cancelled, done, or failed,
                # then we don't know what has happened to the job.
                return JobStatus.UNKNOWN

        # If we have the wrapper we trust him to update last_status and we sent the object's status.
        return job.last_status

    def get_job(self, job_id, *args, **kwargs):
        """
        Get a job
        :job_id: id of the job
        """
        job = Job.query.get(job_id)
        if not job:
            raise JobManagerException("Job not found.")
        return job

    def list_jobs(self, page=None, **kwargs):
        """
        List submitted jobs.
        :param page: page number
        :return: job pagination
        """
        query = Job.query.filter(Job.owner_id == current_user.id).order_by(Job.submit_date.desc())

        return query.paginate(page, app.config['PER_PAGE'])

    def get_file_location(self, job_id, file_name):
        """
        Returns the folder of the file
        :param job_id:
        :param file_name:
        :return:
        """
        return JobFileHandler.get_file_location(job_id, file_name)

    def cancel_job(self, job_id):
        """
        Cancel a job
        :param job_id:
        :return:
        """
        if job_id in self.__jobs:
            wrapper = self.__jobs[job_id]
            wrapper.cancel()
        else:
            raise JobManagerException("Detached or non-existing job.")