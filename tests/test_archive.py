import sys
import pathlib
import pytest
import hashlib
import random
import re

_rundir = pathlib.Path(__file__).parent
if not str( _rundir.parent ) in sys.path:
    sys.path.insert( 0, str( _rundir.parent ) )
from archive import Archive

class ArchiveTestBase:
    @pytest.fixture(scope='class')
    def localfile( self ):
        contents = "".join( random.choices( '0123456789abcdef', k=16 ) )
        filepath = pathlib.Path( "/tmp/test1" ) / ( "".join( random.choices( '0123456789abcdef', k=16  ) ) )
        filepath.parent.mkdir( parents=True, exist_ok=True )
        with open( filepath, "w" ) as ofp:
            ofp.write( contents )
        md5 = hashlib.md5()
        md5.update( contents.encode("ascii") )
        yield contents, filepath, md5.hexdigest()
        filepath.unlink()

    @pytest.fixture(scope='class')
    def upload( self, archive, localfile ):
        contents, localpath, localmd5 = localfile
        serverpath = pathlib.Path( "thing" ) / localpath.name
        yield archive.upload( localpath, "thing", localpath.name )
        archive.delete( serverpath )

    @pytest.fixture(scope='class')
    def upload_and_overwrite( self, archive, localfile, upload ):
        oldcontents, filepath, oldmd5 = localfile
        contents = "".join( random.choices( '0123456789abcdef', k=16 ) )
        newfilepath = pathlib.Path( "/tmp/new_file_to_overwrite_the_old_one" )
        with open( newfilepath, "w" ) as ofp:
            ofp.write( contents )
        md5 = hashlib.md5()
        md5.update( contents.encode("ascii") )
        md5sum = md5.hexdigest()
        assert oldmd5 != md5sum         # This could randomly happen, but hopefully it will never
        servermd5 = archive.upload( newfilepath, "thing", filepath.name, overwrite=True )
        assert md5sum == servermd5
        yield filepath, md5sum
        newfilepath.unlink()
        archive.delete( pathlib.Path( "thing" ) / filepath.name )


    # I'm doing this rather than overriding test_upload in the
    #  derived classes, because I depend on my tests being in the order
    #  they are here.  If I put test_upload in the derived class
    #  show up *after* the methods here in the base class, and the
    #  state of the system will no longer be what's expected
    def additional_test_upload( self, filepath, md5sum ):
        pass

    def test_upload( self, localfile, upload ):
        contents, filepath, md5sum = localfile
        assert md5sum == upload
        self.additional_test_upload( filepath, md5sum )

    def test_getinfo( self, archive, localfile, upload ):
        contents, filepath, md5sum = localfile
        serverpath = pathlib.Path( "thing" ) / filepath.name
        info = archive.get_info( serverpath )
        assert info is not None
        assert info["serverpath"] == f"{self.serverpathbase_ro}/test1/thing/{filepath.name}"
        assert info["size"] == 16
        assert info["md5sum"] == md5sum

    def test_getinfo_missing_file( self, archive ):
        info = archive.get_info( 'thing/this_file_does_not_exist_because_it_has_not_been_created' )
        assert info is None

    def test_download( self, archive, localfile, upload ):
        contents, filepath, md5sum = localfile
        serverpath = pathlib.Path( "thing" ) / filepath.name
        localpath = pathlib.Path( "/tmp/downloaded/thing" ) / filepath.name
        localpath.parent.mkdir( exist_ok=True, parents=True )
        archive.download( serverpath, localpath )
        assert localpath.is_file()
        md5 = hashlib.md5()
        with open( localpath, "rb" ) as ifp:
            md5.update( ifp.read() )
        assert md5.hexdigest() == md5sum

        localpath.unlink()

    def test_not_overwrite( self, archive, localfile, upload ):
        contents, origfile, md5sum = localfile
        serverpath = pathlib.Path( "thing" ) / origfile.name
        newcontents = "".join( random.choices( '0123456789abcdef', k=16 ) )
        filepath = pathlib.Path( "/tmp/test_not_overwrite_temp" )
        with open( filepath, "w" ) as ofp:
            ofp.write( newcontents )

        try:
            archive.upload( filepath, serverpath.parent, serverpath.name, overwrite=False )
        except Exception as ex:
            match = re.search( r"^Error response from .*: Can't upload .*, the file .* already exists "
                               r"and overwrite is False|"
                               r"^Failed to copy, .* already exists on archive and overwrite was False",
                               str(ex) )
            assert match is not None
        else:
            assert False, "An exception should have been raised"

        filepath.unlink()

    def test_overwrite( self, archive, localfile, upload_and_overwrite ):
        oldcontents, oldpath, oldmd5 = localfile
        filepath, md5sum = upload_and_overwrite
        assert oldpath == filepath
        serverpath = pathlib.Path( "thing" ) / filepath.name
        info = archive.get_info( serverpath )
        assert info is not None
        assert info["md5sum"] == md5sum

    def test_download_noverify( self, archive, localfile, upload_and_overwrite ):
        oldcontents, oldpath, oldmd5 = localfile
        filepath, md5sum = upload_and_overwrite
        serverpath = pathlib.Path( "thing" ) / filepath.name
        archive.download( serverpath, filepath, verifymd5=False )
        md5 = hashlib.md5()
        with open( filepath, "rb" ) as ifp:
            md5.update( ifp.read() )
        assert md5.hexdigest() == oldmd5

    def test_download_verify_noclobber( self, archive, localfile, upload_and_overwrite ):
        oldcontents, oldpath, oldmd5 = localfile
        filepath, md5sum = upload_and_overwrite
        serverpath = pathlib.Path( "thing" ) / filepath.name
        try:
            archive.download( serverpath, filepath, verifymd5=True, clobbermismatch=False )
            assert False, "Should have raised an exception"
        except Exception as ex:
            assert re.search( '^Local file .* exists but md5sum doesn.t match .* on archive;', str(ex) )

    def test_download_verify_clobber( self, archive, localfile, upload_and_overwrite ):
        oldcontents, oldpath, oldmd5 = localfile
        filepath, md5sum = upload_and_overwrite
        serverpath = pathlib.Path( "thing" ) / filepath.name
        archive.download( serverpath, filepath, verifymd5=True, clobbermismatch=True )
        md5 = hashlib.md5()
        with open( filepath, "rb" ) as ifp:
            md5.update( ifp.read() )
        assert md5.hexdigest() == md5sum



class TestRemoteArchive(ArchiveTestBase):
    serverpathbase = "/storage/base"
    serverpathbase_ro = "/storage_ro/base"

    @pytest.fixture(scope='class')
    def tokens( self ):
        tokens = {}
        with open( "/run/secrets/connector_tokens" ) as ifp:
            for line in ifp:
                them = line.strip().split()
                tokens[them[0]] = them[1]
        return tokens

    @pytest.fixture(scope='class')
    def archive( self, tokens ):
        return Archive( archive_url='http://archive-server:8080/',
                        path_base='test1',
                        token=tokens['test1/'],
                        verify_cert=False )

    def test_bad_token( self ):
        bad_token_archive = Archive( archive_url='http://archive-server:8080/',
                                     path_base='test1',
                                     token='this_is_not_the_right_token',
                                     verify_cert=False )
        try:
            info = bad_token_archive.get_info( 'thing/this_file_does_not_exist_because_it_has_not_been_created' )
            assert False, "An exception should have been raised"
        except Exception as ex:
            assert str(ex) == "Invalid token for archive server"

    def test_bad_url_archive( self ):
        bad_url_archive = Archive( archive_url='http://this-is-a-server-that-really-should-not-exist:12345/',
                                   path_base='test1',
                                   token='this_is_not_the_right_token',
                                   verify_cert=False )
        try:
            info = bad_url_archive.get_info( 'thing/irrelevant' )
            assert False, "An exception should have been raised"
        except Exception as ex:
            assert str(ex)[0:35] == "Repeated failures trying to post to"


class TestLocalArchive(ArchiveTestBase):
    serverpathbase = "/local_archive/base"
    serverpathbase_ro = "/local_archive/base"

    @pytest.fixture(scope='class')
    def archive( self ):
        return Archive( archive_url=None,
                        path_base='test1',
                        local_read_dir=self.serverpathbase )

    def test_local_write_defaults_to_local_read( self, archive ):
        assert archive.local_write_dir == archive.local_read_dir

    def additional_test_upload( self, filepath, md5sum ):
        md5 = hashlib.md5()
        with open( pathlib.Path( "/local_archive/base/test1/thing" ) / filepath.name, "rb" ) as ifp:
            md5.update( ifp.read() )
        assert md5.hexdigest() == md5sum


class TestLocalArchiveDiffReadWrite(ArchiveTestBase):
    serverpathbase = "/local_archive/base"
    serverpathbase_ro = "/local_archive_ro/base"

    @pytest.fixture(scope='class')
    def archive( self ):
        return Archive( archive_url=None,
                        path_base='test1',
                        local_read_dir=self.serverpathbase_ro,
                        local_write_dir=self.serverpathbase )

    def test_cannot_write_to_ro( self ):
        with pytest.raises( OSError ):
            with open( f"{self.serverpathbase_ro}/junkfile", "wb" ) as ofp:
                ofp.write( "This should never happen" )

    def additional_test_upload( self, filepath, md5sum ):
        md5 = hashlib.md5()
        with open( pathlib.Path( "/local_archive/base/test1/thing" ) / filepath.name, "rb" ) as ifp:
            md5.update( ifp.read() )
        assert md5.hexdigest() == md5sum
