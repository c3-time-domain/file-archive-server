import sys
import os
import io
import re
import json
import pathlib
import traceback
import logging
import hashlib
import shutil

import flask
import flask.views
import flask_session

# ======================================================================

def setup_flask_app( application ):
    # urls are defined at the very bottom of the file so all
    #   class definitions will be in place upon the first
    #   parsing pass of the file.
    global urls
    global pathtokens
    global pathregex
    global nodotfiles

    pathregex = re.compile( r"^[0-9a-zA-Z-_=/\.\$\: ]+$" )
    nodotfiles = re.compile( r"^\.|/\." )

    noparent = re.compile( r"(^\.\./)|(/\.\.)" )

    pathtokens = {}
    secretdir = pathlib.Path( os.getenv( "CONNECTOR_SECRETS", "/run/secrets" ) )
    tokenregex = re.compile( r"^([^ ]+) *(.*)$" )
    with open( secretdir / "connector_tokens" ) as ifp:
        lines = ifp.readlines()
        for line in lines:
            line = line.strip()
            match = tokenregex.search( line )
            if match is None:
                sys.stderr.write( f"Failed to parse path/token line \"{line}\"!\n" )
            else:
                pathtokens[ match[1] ] = match[2]
    # sys.stderr.write( f"Read pathtokens: {pathtokens}\n" )

    secretdir = pathlib.Path( os.getenv( "CONNECTOR_SECRETS", "/run/secrets" ) )
    with open( secretdir / "flask_secret_key" ) as ifp:
        secret_key = ifp.readline().strip()

    application.config.from_mapping(
        SECRET_KEY=secret_key,
        SESSION_COOKIE_PATH='/',
        SESSION_TYPE='filesystem',
        SESSION_PERMANENT=True,
        SESSION_USE_SIGNER=True,
        SESSION_FILE_DIR=os.getenv( "CONNECTOR_SESSION_DIR", "/sessions" ),
        SESSION_FILE_THRESHOLD=1000
    )

    _server_session = flask_session.Session( application )

    usedurls = {}
    for url, cls in urls.items():
        if url not in usedurls.keys():
            usedurls[ url ] = 0
            name = url
        else:
            usedurls[ url ] += 1
            name = f'{url}.{usedurls[url]}'

        application.add_url_rule( url, view_func=cls.as_view(name), methods=['GET', 'POST'], strict_slashes=False )


# ======================================================================

class Failure(Exception):
    def __init__( self, errormsg ):
        self.message = errormsg
        self.errorjson = { "status": "error", "error": errormsg }

    def __str__( self ):
        return self.message


# ======================================================================

class BaseView( flask.views.View ):

    def __init__( self, *args, **kwargs ):
        super().__init__( *args, **kwargs )

    def mkdir( self, direc, dirmode=None ):
        if direc.is_dir():
            return
        if direc.exists() and ( not direc.is_dir() ):
            raise Failure( f'{str(direc)} exists and is not a directory.' )
        else:
            if dirmode is None:
                dirmode = 0o755
            direc.mkdir( parents=True, exist_ok=True )
            direc.chmod( int(dirmode) )

    def dispatch_request( self, *args, **kwargs ):
        global pathtokens
        global pathregex
        global nodotfiles

        self.read_storage = pathlib.Path( os.getenv( "CONNECTOR_READ_STORAGE", "/dest" ) )
        self.write_storage = pathlib.Path( os.getenv( "CONNECTOR_WRITE_STORAGE", "/dest" ) )
        self.secretdir = pathlib.Path( os.getenv( "CONNECTOR_SECRETS", "/run/secrets" ) )
        self.app = flask.current_app
        self.logger = self.app.logger

        try:
            self.data = { 'path': None,
                          'targetoflink': None,
                          'mode': None,
                          'dirmode': None,
                          'overwrite': 0,
                          'okifmissing': 0,
                          'token': None
                         }
            if flask.request.is_json:
                self.data.update( flask.request.json )
            elif ( ( flask.request.content_type[0:19] == "multipart/form-data" ) or
                   ( flask.request.content_type[0:33] == "application/x-www-form-urlencoded" ) ):
                self.data.update( flask.request.form )
            else:
                raise Failure( f"Unknown content-type {flask.request.content_type}, "
                               f"expected json, x-www-form-urlencoded, or multipart/form-data" )

            if self.data["path"] is None:
                raise Failure( "No path specified." )
            if ( not pathregex.search( self.data["path"] ) ) or ( nodotfiles.search( self.data["path"] ) ):
                raise Failure( f"Invalid path {self.data['path']}, filenames can only include "
                               f"0-9, a-z, A-Z, -, _, =, $, ., :, and /.  Dot files aren't allowed." )
            if self.data["token"] is None:
                raise Failure( "No token specified." )
            ok = False
            for path, token in pathtokens.items():
                if self.data["path"][0:len(path)] == path:
                    ok = True
                    if token != self.data["token"]:
                        self.logger.error( f"Was passed token {self.data['token']} for path {self.data['path']}, "
                                               f"expected {token} for {path}" )
                        raise Failure( f"Invalid token for {self.data['path']}" )
            if not ok:
                raise Failure( f"Unknown path {self.data['path']}" )

            return self.do_the_things( *args, **kwargs )

        except Failure as ex:
            return ex.errorjson
        except Exception as ex:
            strerr = io.StringIO()
            traceback.print_exc( file=strerr )
            self.logger.error( strerr.getvalue() )
            return f"Something bad happened:\n{ex}", 500

    def do_the_things( self, *args, **kwargs):
        raise Failure( f"{self.__class__.__name__} needs to implement do_the_things" )


# ======================================================================

class UploadFile( BaseView ):
    def do_the_things( self ):
        if len( flask.request.files ) != 1:
            raise Failure( f"Upload expected 1 file, got {len(flask.request.files)}" )
        if 'fileinfo' not in flask.request.files:
            raise Failure( f"Expected the file to have key {'fileinfo'}, got something else." )
        fileinfo = flask.request.files[ 'fileinfo' ]

        outpath = self.write_storage / self.data["path"]
        if outpath.exists():
            if not outpath.is_file():
                raise Failure( f"Can't upload {self.data['path']}, the file {outpath} already exists "
                               f"and is not a regular file!" )
            if not int(self.data['overwrite']):
                raise Failure( f"Can't upload {self.data['path']}, the file {outpath} already exists "
                               f"and overwrite is False" )

        self.mkdir( outpath.parent, self.data['dirmode'] )
        fileinfo.save( outpath )
        fileinfo.close()

        archivesize = os.stat( outpath ).st_size
        if archivesize != int( self.data["size"] ):
            raise Failure( f'Size of written file {archivesize} doesn\'t match expected size {self.data["size"]}' )
        if ( "mode" in self.data ) and ( self.data["mode"] is not None ):
            outpath.chmod( int( self.data["mode"] ) )
        md5 = hashlib.md5()
        with open( outpath, "rb" ) as ifp:
            md5.update( ifp.read() )
        md5sum = md5.hexdigest()
        if "md5sum" in self.data and self.data["md5sum"] is not None:
            if md5sum != self.data["md5sum"]:
                shutil.copy2( outpath, outpath.parent / f'{outpath.name}.FAIL' )
                outpath.unlink()
                raise Failure( f"md5sum of file {md5sum} doesn't match "
                               f"passed md5sum {self.data['md5sum']}, file not written" )

        return { 'status': 'File uploaded',
                 'filename': outpath.name,
                 'path': str(outpath),
                 'length': archivesize,
                 'md5sum': md5sum
                }


# ======================================================================

class GetFileInfo( BaseView ):
    def do_the_things( self ):
        path = self.read_storage / self.data["path"]
        if not path.is_file():
            raise Failure( f"No such file {path}" )
        md5 = hashlib.md5()
        with open( path, "rb" ) as ifp:
            md5.update( ifp.read() )
        stat = path.stat()
        return { "serverpath": str(path),
                 "size": stat.st_size,
                 "md5sum": md5.hexdigest()
                }


# ======================================================================

class DownloadFile( BaseView ):
    def do_the_things( self ):
        path = self.read_storage / self.data["path"]
        if not path.is_file():
            raise Failure( f"No such file {path}" )
        with open( path, "rb" ) as ifp:
            filedata = ifp.read()
        return flask.Response( filedata, content_type="application/octet-stream" )


# ======================================================================

class MakeLink( BaseView ):
    def do_the_things( self ):
        path = self.write_storage / self.data["path"]
        if ( not self.data["overwrite"] ) and ( path.exists() ):
            raise Failure( f"File already exists: {path}" )
        self.mkdir( path.parent, self.data["dirmode"] )
        if not self.data["targetoflink"].exists():
            raise Failure( f"Link target doesn't exist: {self.data['targetoflnk']}" )
        path.symlink_to( self.data['targetoflink'] )
        return { 'status': 'Link created',
                 'target': str(self.data["targetoflink"]),
                 'link': str(path)
                }


# ======================================================================

class DeleteFile( BaseView ):
    def do_the_things( self ):
        path = self.write_storage / self.data["path"]
        if not int(self.data['overwrite']):
            raise Failure( "Not deleting file, overwrite is False" )
        if not path.exists():
            if not self.data['okifmissing']:
                raise Failure( f"Failed to delete file that doesn't exist: {str(path)}" )
        else:
            if path.is_dir():
                raise Failure( f"{path} is a directory" )
            else:
                path.unlink()
        return { "status": "File deleted",
                 "filename": path.name,
                 "path": str(path)
                 }


# ======================================================================

class FileArchiveServer( BaseView ):
    def dispatch_request( self, *args, **kwargs) :
        return "File Archive Server."


# ======================================================================

urls = {
    '/upload': UploadFile,
    '/getfileinfo': GetFileInfo,
    '/download': DownloadFile,
    '/makelink': MakeLink,
    '/delete': DeleteFile,
    '/': FileArchiveServer
}

pathtokens = {}
pathregex = None
nodotfiles = None

application = flask.Flask( __name__ )
application.logger.setLevel( logging.INFO )
# application.logger.setLevel( logging.DEBUG )

setup_flask_app( application )
