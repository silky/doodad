"""
These objects are pointers to code/data you wish to give access
to a launched job.

Each object defines a source and a mount point (where the directory will be visible
to the launched process)

"""
import os
import shutil
import tarfile
import tempfile
from contextlib import contextmanager

from doodad.ec2 import aws_util
from doodad.launch import utils


class Mount(object):
    """
    Args:
        mount_point (str): Location of directory visible to the running process
        pythonpath (bool): If True, adds this folder to the $PYTHON_PATH environment variable
        output (bool): If False, this is a "code" directory. If True, this should be an empty
            "output" directory (nothing will be copied to remote)
    """
    def __init__(self, mount_point=None, pythonpath=False, output=False):
        self.pythonpath = pythonpath
        self.read_only = not output
        self.mount_point = mount_point
        self.__name = None
        self.local_dir = None

    def dar_build_archive(self, deps_dir):
        raise NotImplementedError()

    def dar_extract_command(self):
        raise NotImplementedError()

    @property
    def name(self):
        return self.__name

    @contextmanager
    def gzip(self, filter_ext=(), filter_dir=()):
        """
        Return filepath to a gzipped version of this directory for uploading
        """
        assert self.read_only
        def filter_func(tar_info):
            filt = any([tar_info.name.endswith(ext) for ext in filter_ext]) or \
                   any([ tar_info.name.endswith('/'+ext) for ext in filter_dir])
            if filt:
                return None
            return tar_info
        with tempfile.NamedTemporaryFile('wb+', suffix='.tar') as tf:
            # make a tar.gzip archive of directory
            with tarfile.open(fileobj=tf, mode="w") as tar:
                tar.add(self.local_dir, arcname=os.path.basename(self.local_dir), filter=filter_func)
            tf.seek(0)
            yield tf.name

class MountLocal(Mount):
    def __init__(self, local_dir, mount_point=None, cleanup=True,
                filter_ext=('.pyc', '.log', '.git', '.mp4'),
                filter_dir=('data',),
                **kwargs):
        super(MountLocal, self).__init__(mount_point=mount_point, **kwargs)
        self.local_dir = os.path.realpath(os.path.expanduser(local_dir))
        self.__name = self.local_dir.replace('/', '_')
        self.local_dir_raw = local_dir
        self.cleanup = cleanup
        self.filter_ext = filter_ext
        self.filter_dir = filter_dir
        if mount_point is None:
            self.mount_point = mount_point
            self.no_remount = True
        else:
            self.no_remount = False

    def dar_build_archive(self, deps_dir):
        # make a symlink to deps dir
        dep_dir = os.path.join(deps_dir, 'local', self.name)
        os.makedirs(os.path.join(deps_dir, 'local'))
        #os.symlink(self.local_dir, dep_dir)
        shutil.copytree(self.local_dir, dep_dir)

    def dar_extract_command(self):
        # make a symlink to mount dir
        return 'ln -s ./deps/local/{dep_name} {mount}'.format(
            dep_name=self.name,
            mount=self.mount_point
        )

    def __str__(self):
        return 'MountLocal@%s'%self.local_dir

    def docker_mount_dir(self):
         return os.path.join('/mounts', self.mount_point.replace('~/',''))


class MountGitRepo(Mount):
    def __init__(self, git_url, branch=None,
                 git_credentials=None, **kwargs):
        super(MountGitRepo, self).__init__(read_only=True, **kwargs)
        self.git_url = git_url
        self.git_credentials = git_credentials
        raise NotImplementedError()


class MountS3(Mount):
    def __init__(self, 
                region,
                s3_bucket,
                s3_path, 
                local_dir=None,
                sync_interval=15, 
                output=False,
                include_types=('*.txt', '*.csv', '*.json', '*.gz', '*.tar', '*.log', '*.pkl'), 
                **kwargs):
        super(MountS3, self).__init__(output=output, **kwargs)
        # load from config
        self.s3_bucket = s3_bucket
        self.s3_path = s3_path
        self.region = region
        self.output = output
        self.sync_interval = sync_interval
        self.sync_on_terminate = True
        self.include_types = include_types
        self.__name = '%s.%s' % (self.s3_bucket, self.s3_path.replace('/', '.'))
        if output is False:
            assert local_dir is not None
            self.local_dir = os.path.realpath(os.path.expanduser(local_dir))

    def __str__(self):
        return 'MountS3@s3://%s/%s'% (self.s3_bucket, self.s3_path)

    @property
    def include_string(self):
        return ' '.join(['--include \'%s\''%type_ for type_ in self.include_types])

    def s3_upload(self, filename, remote_filename=None, dry=False, check_exist=True):
        if remote_filename is None:
            remote_filename = os.path.basename(filename)
        remote_path = 'doodad/mount/'+remote_filename
        if check_exist:
            if aws_util.s3_exists(self.s3_bucket, remote_path, region=self.region):
                print('\t%s exists! ' % os.path.join(self.s3_bucket, remote_path))
                return 's3://'+os.path.join(self.s3_bucket, remote_path)
        return aws_util.s3_upload(filename, self.s3_bucket, remote_path, dry=dry,
                         region=self.region)

    def dar_build_archive(self, deps_dir):
        # first upload directory to S3
        with self.gzip() as gzip_file:
            gzip_path = os.path.realpath(gzip_file)
            file_hash = utils.hash_file(gzip_path)
            s3_path = self.s3_upload(gzip_path, remote_filename=file_hash+'.tar')
        self.path_on_remote = s3_path
        self.local_file_hash = gzip_path

        dep_dir = os.path.join(deps_dir, 's3', self.name)
        os.makedirs(os.path.join(deps_dir, 's3'))

        with open(os.path.join(dep_dir, 'extract.sh'), 'w') as f:
            f.write('')
            remote_tar_name = '/tmp/'+file_hash+'.tar'
            mount_point =  os.path.join('/mounts', self.mount_point.replace('~/',''))
            #remote_unpack_name = '/tmp/'+file_hash
            f.write("aws s3 cp {s3_path} {remote_tar_name}\n".format(s3_path=s3_path, remote_tar_name=remote_tar_name))
            f.write("mkdir -p {local_code_path}\n".format(local_code_path=mount_point))
            f.write("tar -xvf {remote_tar_name} -C {local_code_path}\n".format(
                local_code_path=mount_point,
                remote_tar_name=remote_tar_name))

    def dar_extract_command(self):
        # execute the script to pull from S3
        if self.output is True:
            raise NotImplementedError()
        return './deps/s3/{name}/extract.sh'.format(
            name=self.name,
        )