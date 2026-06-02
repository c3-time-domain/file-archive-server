import logging
import hashlib
import pathlib
import requests
import json
import shutil
import time
import os
import functools
import multiprocessing


class _SleptTooLongError( RuntimeError ):
    pass


class Archive:
    """A class for communcation with an archive.

    Supports two different archives: a local directory, and a server
    running the server code at
    https://github.com/c3-time-domain/nersc-upload-connector.  Normally
    you will only use one interface.  See __init__ for configuring where
    the archive exists.

    Call upload() to push something to the archive, get_info() to get
    information about something on the archive, and download() to pull
    something from the archive.

    """

    def __init__( self,
                  archive_url=None,
                  path_base=None,
                  token=None,
                  verify_cert=True,
                  local_read_dir=None,
                  local_write_dir=None,
                  sleeptime=2,
                  retries=5,
                  lockfunc=None,
                  logger=logging.getLogger("main") ):
        """Construct an Archive object.

        Parameters
        ----------
          archive_url : str
             The URL of the webap on the server running the
             upload-connector code.  If None, then local_read_dir must
             not be None.

          path_base : str
             The base path, or "collection", that we're archiving to.
             This is a path relative to the archive server's global
             root.  It exists so that different instantiations of a
             database can use the same archive server without confusing
             their files.

          token : str
             The authentication token for the server that corresponds to
             path_base

          verify_cert : bool
             If False, don't bother verifying the server's SSL
             certificate (i.e. live dangerously).

          local_read_dir : str
             A locally-available directory that serves as the archive;
             path_base must be a subdirectory there.  Files will be read
             from here.  Should normally be None if archive_url is not
             None.

          local_write_dir : str
             When using a local archive, the base directory to where all
             archive files are written.  If passed as None (which will
             usually be the case), the code will make it the same as
             local_read_dir.  This option exists in case the system has
             two different ways of getting at the filesystem, one that's
             more efficient for reading (which is the case, for
             instance, on NERSC CFS as of Jan. 2024.)

          sleeptime : int or float, default 2
            Time to sleep (in seconds) after a failure before retrying.

          retries : int, default 5
            Number of times to retry if there's a communications failure.

          lockfunc : callable or None
            If not None, must be a function that takes a single string
            argument (the server path), plus an optional positional
            boolean argument unlock.  The function will do whatever is
            necessary to get an exclusive lock on whatever resource is
            associated with that string argument.  When called with
            unlock, it releases that lock.  Used in an attempt to deal
            with multiple processes trying to download the same file all
            at once.  If nothing is given, than use an imperfect
            built-in implenetation that still has some race conditions
            left.  (Actually, that's not implemented yet, so the current
            default is a null-op!)


          logger : logging.Logger
             Defaults to getting the logger "main".

        Configure the archive on instantiation to use the web server by
        passing a non-None archive_url.  Configure the archive to use the
        local filesystem by passing a non-None local_read_dir.

        It usually only makes sense to have either archive_url, or
        local_read_dir, set, but not both.  archive_url should work from
        anywhere the archive web server is accessible.  local_read_dir and
        local_write_dir are present as a potential performance boost in the
        special case where the archive web server's storage directories are
        mounted on the same machine as where the client Archive class is
        running.

        When uploading, if both archive_url and local_write_dir are set, the
        code will write files to *both* locations.  In the case where the
        archive server's storage directories are mounted on the client
        machine, this means redudant writes, which is not what you want.

        When downloading, if both archive_url and local_read_dir are set,
        the archive will first try to get the file from local_read_dir, and
        then if that fails, will go to the archive server specified in
        archive_url to try to get the file.

        (Normally, local_read_dir and local_write_dir are the same, and if
        local_write_dir is None, it will be set to be local_read_dir.  The
        reason they're two different things is in case there are two
        different ways to access the same filesystem, one optimized for
        reading; this is the case on the NERSC CFS filesystem as of Jan
        2024.)

        """
        if ( local_read_dir is None) and ( archive_url is None ):
            raise ValueError( "Archive: one of local_read_dir or archive_url must be non-None" )
        if local_write_dir is None:
            local_write_dir = local_read_dir

        self.logger = logger
        self.url = archive_url
        if ( self.url is not None ) and ( self.url[-1] == '/' ):
            self.url = self.url[:-1]
        self.path_base = pathlib.Path( path_base )
        self.token = token
        self.local_read_dir = None if local_read_dir is None else pathlib.Path( local_read_dir )
        self.local_write_dir = None if local_write_dir is None else pathlib.Path( local_write_dir )
        if ( self.local_write_dir is None ) and ( self.local_read_dir is not None ):
            self.local_write_dir = self.local_read_dir
        self.sleeptime = sleeptime
        self.lockfunc = ( lockfunc if lockfunc is not None else
                          functools.partial( self.__class__._lockfunc, self ) )
        self.retries = retries
        self.verify_cert = verify_cert

    # ======================================================================

    def _retry_request( self, endpoint, data={}, filepath=None, isjson=True, downloadfile=None,
                        retries=None, sleeptime=None, expectederror=None ):
        """Send a request to the archive server with retries.

        Parameters
        ----------
          endpoint : string
            The part of the URL after self.url

          data : dict
            Post data; a dict that will be json encoded by passing it to
            the json= argument of requests.post

          filepath : pathlib.Path or str
            Path of file to upload, or None (default).

          isjson : bool
            True if we expect a json response, false otherwise (default
            True).

          downloadfile : pathlib.Path or str
            Path of binary file to download, or None if none is expected
            (default None)

          retries : int, default configured at object init
            Number of times to retry if there's a communications failure.

          sleeptime : int or float, default configure at object init
            Time to sleep (in seconds) after a failure before retrying.

          expectederror : str
            A string to match an error response from the server (not an
            http error, but a succesful web request with an embedded
            error messge).  If the message matches, this funtion won't
            raise an Exception, but will return None.  (See below.)

        Returns
        -------
          If succesful, will return the data structure loaded from the
          returned json (if isjson is True) or True (if downloadfile is
          not None).

        If the first try returns an error response (so, a valid return
        from the server, but with a json encoded dictionary that has an
        "error" field), and if expectederror is not None, and the
        beginning of the value of the "error" field of the returned
        dictionary matches expectederror, returns None.

        Otherwise, on repeated failures, will raise an exception.

        """

        url = f"{self.url}/{endpoint}"
        if ( not isjson ) and ( downloadfile is None ):
            raise RuntimeError( "isjson is false, and downloadfile is None... I don't know what to do with {url}" )

        sleeptime = sleeptime if sleeptime is not None else self.sleeptime
        retries = retries if retries is not None else self.retries
        countdown = retries
        ifp = None
        while countdown >= 0:
            res = None
            try:
                ifp = None
                files = None
                if filepath is not None:
                    ifp = open( filepath, "rb" )
                    files = { "fileinfo": ifp }
                self.logger.debug( f"Archive posting to {url} with filepath {filepath} and data {data}" )
                sendtime = time.perf_counter()
                res = requests.post( f"{url}", data=data, files=files, verify=self.verify_cert )
                if ifp is not None:
                    ifp.close()
                    ifp = None
            except Exception as ex:
                dt = time.perf_counter() - sendtime
                self.logger.warning( f"Got exception {ex} after {dt:.2f}s trying to contact {url} with data {data}" )
            else:
                dt = time.perf_counter() - sendtime
                if res.status_code != 200:
                    self.logger.warning( f"Got status_code={res.status_code} from {url} after {dt:.2f}s "
                                         f"with data {data} : {res.text}" )
                elif isjson:
                    if res.headers['content-type'] != 'application/json':
                        self.logger.warning( f"Server returned {res.headers['content-type']} after {dt:.2f}s, "
                                             f"expected json" )
                    else:
                        try:
                            resval = json.loads( res.text )
                            self.logger.debug( f"Got successful response from {url} after {dt:.2f}s: {resval}" )
                        except Exception:
                            self.logger.warning( f"Failed to load JSON from {res.text}" )
                        else:
                            if "error" in resval:
                                if ( ( expectederror is not None) and
                                     ( resval['error'][:len(expectederror)] == expectederror ) ):
                                    return None
                                if resval['error'][0:13] == 'Invalid token':
                                    self.logger.error( f"Invalid token for {url}" )
                                    raise RuntimeError( "Invalid token for archive server" )
                                else:
                                    tb = resval['traceback'] if 'traceback' in resval else '(No traceback)'
                                    self.logger.error( f"Got error response {resval['error']} from {url} "
                                                       f"with data {data}\n{tb}" )
                                    raise RuntimeError( f"Error response from {url}: {resval['error']}" )
                            else:
                                return resval
                elif downloadfile is not None:
                    self.logger.debug( f"Got {len(res.content)} bytes of download back from {url} after {dt:.2f}s" )
                    if res.headers['content-type'] != 'application/octet-stream':
                        self.logger.warning( f"Server returned {res.headers['content-type']}, "
                                             f"expected an octet stream" )
                    else:
                        with open( downloadfile, "wb" ) as ofp:
                            ofp.write( res.content )
                        return True
                else:
                    raise RuntimeError( "This should never happen." )
            finally:
                try:
                    if ifp is not None:
                        ifp.close()
                    res.close()
                except Exception:
                    pass

            # If we haven't returned, then it's an error of some sort, and we should keep counting down
            countdown -= 1
            if countdown >= 0:
                self.logger.warning( f"Failed to post to {url} with data {data}; "
                                     f"will sleep {sleeptime}s and retry." )
                time.sleep( sleeptime )

        raise RuntimeError( f"Repeated failures trying to post to {url} with data {data}" )

    # ======================================================================

    def upload( self, localpath, remotedir=None, remotename=None, overwrite=True, md5=None ):
        """Upload/copy a file to the archive.

        Parameters
        ---------
          localpath : str or pathlib.Path
            Path of the file on the local filesystem.

          remotedir : str or pathlib.Path
            Relative path (underneath self.path_base) of the directory
            where the file should live on the archive.

          remotename : str
            The name of the file on the archive.  Defaults to the
            filename part of localpath.

          overwrite : bool, default True
            Should we overwrite the archive file if it already exists?

          md5 : hashlib.has
            The md5sum of the the localpath.  If None, this function
            will calculate it.  If not-None, this function trusts the
            caller to have done it right.

        Retruns
        -------
           md5sum : str
             The md5sum hex digest of the file in the archive if
             succesful.  (Raises an exception if not.)

        """

        localpath = pathlib.Path( localpath )
        remotename = remotename if remotename is not None else localpath.name
        if remotedir is not None:
            serverpath = self.path_base / remotedir / remotename
        else:
            serverpath = self.path_base / remotename

        if not localpath.is_file():
            raise FileNotFoundError( f"Can't find file {localpath} to upload to archive!" )
        localsize = os.stat( localpath ).st_size
        if md5 is None:
            md5 = hashlib.md5()
            with open( localpath, "rb" ) as ifp:
                md5.update( ifp.read() )
        localmd5 = md5.hexdigest()
        md5sum = None

        if self.local_write_dir is not None:
            destpath = self.local_write_dir / serverpath
            if destpath.exists():
                if not destpath.is_file():
                    raise RuntimeError( f"Failed to copy to archive; {destpath} exists and isn't a normal file!" )
                if overwrite:
                    destpath.unlink()
                else:
                    raise RuntimeError( f"Failed to copy, {destpath} already exists on archive "
                                        f"and overwrite was False" )
            if destpath.parent.exists() and not destpath.parent.is_dir():
                raise RuntimeError( f"Failed to copy to archive; destination directory {destpath.parent} "
                                    f"exists, but is not a directory!" )
            destpath.parent.mkdir( parents=True, exist_ok=True )
            shutil.copy2( localpath, destpath )
            md5 = hashlib.md5()
            with open( destpath, "rb" ) as ifp:
                md5.update( ifp.read() )
            md5sum = md5.hexdigest()
            if md5sum != localmd5:
                destpath.unlink()
                raise RuntimeError( f"Tried to copy {localpath} to {destpath}, but destination file had "
                                    f"md5sum {md5sum}, which doesn't match source {localmd5}" )

        if self.url is not None:
            data = { "overwrite": int(overwrite),
                     "path": str(serverpath),
                     "dirmode": 0o755,
                     "mode": 0o644,
                     "token": self.token,
                     "size": localsize,
                     "md5sum": localmd5 }
            resval = self._retry_request( "upload", data=data, filepath=localpath,
                                          expectederror='File already exists' )
            if ( resval is None ) and ( not overwrite ):
                raise RuntimeError( f"Failed to upload, {serverpath} already exists on archive "
                                    f"and overwrite was False" )
            md5sum = resval['md5sum']
            if md5sum != localmd5:
                raise RuntimeError( f"Failed to upload {localpath} to server {serverpath}; "
                                    f"server returned md5sum {md5sum}, which doesn't match "
                                    f"local {localmd5}.  This exception never happen; the server "
                                    f"should have already raised an exception from the mismatch." )

        if md5sum is None:
            raise RuntimeError( "This should never happen; md5sum is None at the end of Archive.upload(). "
                                "An exception should already have been raised." )
        return md5sum

    # ======================================================================

    def get_info( self, serverpath ):
        """Get information about a file on the server

        Parameters
        ----------
          serverpath : pathlib.Path or str
            Path on server relative to self.path_base.

        Returns
        -------
          dict or None
            Returns None if the file isn't found on the archive.

            Otherwise, returns a dictionary with:
              serverpath : absolute path of file on archive (string)
              size : size of file on archive
              md5sum : md5sum of file on archive

        """

        if self.local_read_dir is not None:
            archivepath = self.local_read_dir / self.path_base / serverpath
            if not archivepath.exists():
                return None
            if not archivepath.is_file():
                raise RuntimeError( f"Archive file {archivepath} exists but is not a regular file!" )
            md5 = hashlib.md5()
            with open( archivepath, "rb" ) as ifp:
                md5.update( ifp.read() )
            stat = archivepath.stat()
            return { "serverpath": str(archivepath),
                     "size": stat.st_size,
                     "md5sum": md5.hexdigest() }

        else:
            data = { "path": str( self.path_base / serverpath ), "token": self.token }
            res = self._retry_request( "getfileinfo", data=data, expectederror='No such file' )
            return res

    # ======================================================================

    def delete( self, serverpath, okifmissing=True ):
        """Delete a file in the archive

        Parameters
        ----------
          serverpath : pathlib.Path or str
            Path of file relative to self.path_base to delete on the
            archive server.

          okifmissing : bool, default True
            If False, then raise an exception if the file isn't present
            on the archive.

        Retruns
        -------
          True if it thinks it worked, otherwise raises an exception

        """

        if self.local_write_dir is not None:
            archivepath = self.local_write_dir / self.path_base / serverpath
            if archivepath.exists():
                if not archivepath.is_file():
                    raise RuntimeError( f"Archive file {archivepath} exists but is not a regular file!" )
                archivepath.unlink()
            elif not okifmissing:
                raise FileNotFoundError( f"Can't delete archive file {archivepath}, it doesn't exist." )

        if self.url is not None:
            archivepath = self.path_base / serverpath
            data = { "path": str(archivepath),
                     "token": self.token,
                     "overwrite": 1,
                     "okifmissing": okifmissing
                    }
            self._retry_request( "delete", data=data )

        return True


    # ======================================================================

    def _lockfunc( self, filepath, unlock=False ):
        if not unlock:
            self.logger.warning( "No lockfunc supplied, not dealing with race conditions of multiple "
                                 "processes download the same file at the same time." )
        # TODO... write something


    # ======================================================================

    def download( self, serverpath, localpath, verifymd5=False, clobbermismatch=True, mkdir=True, sleeptest=None ):
        """Copy a file from the archive to local storage.

        Parmaeters
        ----------
          serverpath : str or pathlib.Path
            Path of file relative to self.path_base on the archive server.

          localpath : str or pathlib.Path
            Absolute path where the file should be saved locally.

          verifymd5 : bool, default False
            If False, and the file already exists locally, don't do
            anything.  If True, and the file already exists locally,
            will ask the archive for the corresponding file's md5sum to
            compare to the local file's md5sum.  Subsequent behavior
            depends on the archive's response on the value of
            clobbermismatch.

          clobbermismatch : bool, default True
            If verifymd5 is True and the archive's md5sum doesn't match
            the local file's mismatch, then if this is True, overwrite
            the local file with the one from the server; otherwise,
            raise an exception.

          mkdir : bool, default True
            If localpath's parent directory doesn't already exist, make
            it.  (If you set this to fall, the function might error
            out.)

          sleeptest : float or None
            Here for testing lock functionality; generally don't use this

        Returns
        -------
          True if succesful, otherwise raises an exception.

        """

        localpath = pathlib.Path( localpath )
        if mkdir:
            localpath.parent.mkdir( parents=True, exist_ok=True )

        try:
            self.lockfunc( str(serverpath) )
            if sleeptest is not None:
                time.sleep( sleeptest )

            localmd5 = None
            if localpath.exists():
                if not localpath.is_file():
                    raise RuntimeError( f"{localpath} exists but isn't a regular file!" )
                elif not verifymd5:
                    return True
                md5 = hashlib.md5()
                with open( localpath, "rb" ) as ifp:
                    md5.update( ifp.read() )
                localmd5 = md5.hexdigest()

            serverpath = self.path_base / serverpath

            finished = False

            if self.local_read_dir is not None:
                srcpath = pathlib.Path( self.local_read_dir ) / serverpath
                if not srcpath.exists():
                    raise FileNotFoundError( f"Could not find archive file {serverpath}" )
                md5 = hashlib.md5()
                with open( srcpath, "rb" ) as ifp:
                    md5.update( ifp.read() )
                md5sum = md5.hexdigest()

                if ( localmd5 is not None ) and ( localmd5 != md5sum ):
                    if clobbermismatch:
                        localpath.unlink()
                    else:
                        raise RuntimeError( "Local file {localpath} exists but md5sum doesn't match "
                                            "{srcpath} on archive; local={localmd5}, archive={md5sum}" )

                # If we get this far and localfile exists, then we know we don't want to overwrite it
                if not localpath.exists():
                    shutil.copy2( self.local_read_dir / serverpath, localpath )
                    md5 = hashlib.md5()
                    with open( localpath, "rb" ) as ifp:
                        md5.update( ifp.read() )
                    localmd5 = md5.hexdigest()
                    if localmd5 != md5sum:
                        localpath.unlink()
                        raise RuntimeError( "Error copying from archive {serverpath} to {localpath}; "
                                            "md5sum mismatch: archive {md5sum}, local {md5.hexdigest()}" )
                finished = True

            if ( not finished ) and ( self.url is None ):
                raise RuntimeError( "Haven't been able to copy file from local archive, and there's no url!" )

            if not finished:
                data = { "path": str(serverpath), "token": self.token }
                resval = self._retry_request( "getfileinfo", data=data )
                md5sum = resval['md5sum']
                if localmd5 is not None:
                    if localmd5 != md5sum:
                        if clobbermismatch:
                            localpath.unlink()
                        else:
                            raise RuntimeError( f"Local file {localpath} exists but md5sum doesn't match "
                                                f"{serverpath} on archive; local={localmd5}, server={md5sum}" )

                # If we get this far and localpath exists, we know we're done
                if not localpath.exists():
                    self._retry_request( "download", data=data, isjson=False, downloadfile=localpath )
                    md5 = hashlib.md5()
                    with open( localpath, "rb" ) as ifp:
                        md5.update( ifp.read() )
                    localmd5 = md5.hexdigest()
                    if md5sum != localmd5:
                        localpath.unlink()
                        raise RuntimeError( f"Failed to download archive file {serverpath} to {localpath}; "
                                            f"local md5sum {md5.hexdigest()} did not match server's {md5sum}" )

            return True

        finally:
            self.lockfunc( str(serverpath), unlock=True )
