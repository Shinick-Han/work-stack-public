"""Prove and bind the registry path chain below the desktop StateRoot.

The knowledge registry stores device-local documents under a directory the host
designates.  Everything at or below that StateRoot has to land where it was
proved to land, and proving a path is not enough on its own: a check describes
the moment it was taken, while the create, the lease open and the atomic
replace happen afterwards.  Planting a junction over a registry directory takes
two steps -- the directory has to be removed or renamed away first, and only
then can a link take its name -- so this module holds each directory open for
the length of an operation and stops that first step outright.

On Windows a binding is a ``CreateFileW`` handle that asks for
FILE_LIST_DIRECTORY and shares everything except delete, which is what makes
removing or renaming the directory fail while the handle is open;
FILE_FLAG_OPEN_REPARSE_POINT opens a junction as itself so it is refused rather
than followed, and every handle is compared with the bound parent it was
supposed to be opened under.  On POSIX a binding is a directory descriptor and
every child operation is performed relative to it with ``O_NOFOLLOW``, which is
race-free by construction.  A platform that offers neither is refused rather
than told a repeated check protects it.

The refusal type lives here because this is the lowest layer of the registry:
``knowledge_registry`` imports it and re-exports it as its public error.
"""

from __future__ import annotations

from contextlib import contextmanager, ExitStack
import errno
import os
from pathlib import Path
import stat

REPARSE_POINT = 0x400
BINDING_ACCESS = 0x0001 | 0x0080            # FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES
BINDING_SHARE = 0x0001 | 0x0002             # read and write; never FILE_SHARE_DELETE
OPEN_EXISTING = 3
BACKUP_SEMANTICS = 0x02000000
OPEN_REPARSE_POINT = 0x00200000
DIRECTORY_ATTRIBUTE = 0x10
ATTRIBUTE_TAG_INFO = 9
PATH_BUFFER_CHARS = 32768
ABSENT_WINDOWS_ERRORS = frozenset({2, 3, 267})   # file, path and directory name errors
DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
STREAM_FLAGS = {'rb': os.O_RDONLY, 'a+b': os.O_RDWR | os.O_CREAT | os.O_APPEND,
                'xb': os.O_WRONLY | os.O_CREAT | os.O_EXCL}
STREAM_MODES = {'rb': 'rb', 'a+b': 'a+b', 'xb': 'wb'}


class KnowledgeRegistryError(ValueError):
    """Closed refusal code; never carries a path, a reason, or document text."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def refuse(code: str) -> None:
    raise KnowledgeRegistryError(code)


def same_root(left: str, right: str) -> bool:
    return os.path.normcase(left) == os.path.normcase(right)


def checked_metadata(metadata):
    """Refuse an entry that describes a link rather than a real file or directory."""

    if metadata is not None and (stat.S_ISLNK(metadata.st_mode) or
                                 getattr(metadata, 'st_file_attributes', 0) & REPARSE_POINT):
        refuse('linked_path_refused')
    return metadata


def link_metadata(path: Path) -> os.stat_result | None:
    """Describe one path component without ever following it.

    ``lstat`` is the point: a junction or a symlink has to be seen as itself,
    so the evidence is read before anything could resolve it away.  ``None``
    means the component is simply absent.
    """

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except NotADirectoryError:
        return None
    except OSError as error:
        raise KnowledgeRegistryError('registry_unavailable') from error
    return checked_metadata(metadata)


def components(anchor: Path, path: Path) -> tuple:
    try:
        return path.relative_to(anchor).parts
    except ValueError as error:
        raise KnowledgeRegistryError('invalid_state_root') from error


def guarded_chain(anchor: Path, directory: Path) -> bool:
    """Refuse a link anywhere between the StateRoot and ``directory``.

    Every component is checked, not just the leaf, because a junction planted
    on ``knowledge`` or on a task partition redirects everything below it.
    ``False`` reports that the chain of real directories stops short.
    """

    current = anchor
    for part in components(anchor, directory):
        current = current / part
        metadata = link_metadata(current)
        if metadata is None or not stat.S_ISDIR(metadata.st_mode):
            return False
    return True


def _refuse_escape(anchor: Path, path: Path) -> None:
    """Confirm the proved chain still lands inside the StateRoot.

    Resolution happens only after every component has been checked as given,
    never before, so this is a second opinion on the lstat evidence rather
    than a substitute that would have washed the junction away.
    """

    try:
        inside = os.path.normcase(os.path.realpath(path))
        root = os.path.normcase(os.path.realpath(anchor))
    except (OSError, ValueError) as error:
        raise KnowledgeRegistryError('registry_unavailable') from error
    if inside != root and not inside.startswith(root.rstrip(os.sep) + os.sep):
        refuse('linked_path_refused')


if os.name == 'nt':
    import ctypes
    from ctypes import wintypes

    KERNEL32 = ctypes.WinDLL('kernel32', use_last_error=True)
    KERNEL32.CreateFileW.restype = wintypes.HANDLE
    KERNEL32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.HANDLE]
    KERNEL32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    KERNEL32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR,
                                                   wintypes.DWORD, wintypes.DWORD]
    KERNEL32.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                      ctypes.c_void_p, wintypes.DWORD]
    KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
    INVALID_HANDLE = ctypes.c_void_p(-1).value

    class _AttributeTag(ctypes.Structure):
        _fields_ = [('attributes', wintypes.DWORD), ('reparse_tag', wintypes.DWORD)]

    def _handle_attributes(handle) -> int:
        information = _AttributeTag()
        if not KERNEL32.GetFileInformationByHandleEx(handle, ATTRIBUTE_TAG_INFO,
                                                     ctypes.byref(information),
                                                     ctypes.sizeof(information)):
            refuse('registry_unavailable')
        return information.attributes

    def _handle_path(handle) -> str:
        """Report where an open handle actually lives, not how it was spelled."""

        buffer = ctypes.create_unicode_buffer(PATH_BUFFER_CHARS)
        length = KERNEL32.GetFinalPathNameByHandleW(handle, buffer, PATH_BUFFER_CHARS, 0)
        if not length or length >= PATH_BUFFER_CHARS:
            refuse('registry_unavailable')
        resolved = buffer.value
        if resolved.startswith('\\\\?\\UNC\\'):
            return '\\\\' + resolved[8:]
        return resolved[4:] if resolved.startswith('\\\\?\\') else resolved

    def _open_handle(path: Path, follow: bool):
        """Open one directory, as itself unless the caller asks to follow it."""

        flags = BACKUP_SEMANTICS if follow else BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        handle = KERNEL32.CreateFileW(str(path), BINDING_ACCESS, BINDING_SHARE, None,
                                      OPEN_EXISTING, flags, None)
        if handle is None or handle == INVALID_HANDLE:
            if ctypes.get_last_error() in ABSENT_WINDOWS_ERRORS:
                return None
            refuse('registry_unavailable')
        return handle


class _Binding:
    """One directory held open for the length of a single operation."""

    def __init__(self, token, path: Path):
        self._token = token
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def child(self, name: str, create: bool):
        """Bind one level down, creating it only when a write asks for it.

        ``None`` reports that the chain of real directories stops short: the
        entry is absent, or it is not a directory at all.
        """

        token = self._open_child(name)
        if token is None and create:
            self._create_child(name)
            token = self._open_child(name)
        if token is None:
            return None
        return type(self)(token, self._path / name)


class _WindowsBinding(_Binding):
    """A directory pinned by a handle that denies rename and delete.

    The access has to include FILE_LIST_DIRECTORY: the sharing rules arbitrate
    read, write and delete only, so a handle asking for attributes alone is
    ignored by them and pins nothing.  Omitting FILE_SHARE_DELETE is what makes
    the pin -- removing or renaming this directory fails while the handle is
    open -- and FILE_FLAG_OPEN_REPARSE_POINT means a junction is opened as
    itself and refused rather than quietly followed.
    """

    def close(self) -> None:
        KERNEL32.CloseHandle(self._token)

    def _create_child(self, name: str) -> None:
        try:
            (self._path / name).mkdir()
        except FileExistsError:
            pass
        except OSError as error:
            raise KnowledgeRegistryError('registry_unwritable') from error

    def _open_child(self, name: str):
        handle = _open_handle(self._path / name, follow=False)
        if handle is None:
            return None
        try:
            attributes = _handle_attributes(handle)
            # The handle's own location, compared with the bound parent it was
            # supposed to be opened under: no path spelling is trusted here.
            if attributes & REPARSE_POINT or not same_root(_handle_path(handle),
                                                           str(self._path / name)):
                refuse('linked_path_refused')
            if not attributes & DIRECTORY_ATTRIBUTE:
                KERNEL32.CloseHandle(handle)
                return None
        except BaseException:
            KERNEL32.CloseHandle(handle)
            raise
        return handle

    def entry(self, name: str):
        return link_metadata(self._path / name)

    def open_stream(self, name: str, mode: str):
        import msvcrt

        target = self._path / name
        stream = target.open(mode)
        try:
            if not same_root(_handle_path(msvcrt.get_osfhandle(stream.fileno())), str(target)):
                refuse('linked_path_refused')
        except BaseException:
            stream.close()
            raise
        return stream

    def replace(self, source: str, target: str) -> None:
        os.replace(self._path / source, self._path / target)

    def unlink(self, name: str) -> None:
        (self._path / name).unlink(missing_ok=True)

    def sync(self) -> None:
        """Windows exposes no portable directory sync; the replace is atomic."""


class _PosixBinding(_Binding):
    """A directory pinned by a descriptor, with every operation relative to it.

    A ``dir_fd`` operation names the directory that was opened rather than the
    path that was spelled, so there is no window to lose: a directory removed
    from under this descriptor keeps the descriptor, and the relative create
    lands in the directory that was proved or fails outright.  ``O_NOFOLLOW``
    refuses a symlink planted at the component as itself.
    """

    def close(self) -> None:
        os.close(self._token)

    def _create_child(self, name: str) -> None:
        try:
            os.mkdir(name, dir_fd=self._token)
        except FileExistsError:
            pass
        except OSError as error:
            raise KnowledgeRegistryError('registry_unwritable') from error

    def _open_child(self, name: str):
        try:
            return os.open(name, DIRECTORY_FLAGS | os.O_NOFOLLOW, dir_fd=self._token)
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise KnowledgeRegistryError('linked_path_refused') from error
            raise KnowledgeRegistryError('registry_unavailable') from error

    def entry(self, name: str):
        try:
            metadata = os.stat(name, dir_fd=self._token, follow_symlinks=False)
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as error:
            raise KnowledgeRegistryError('registry_unavailable') from error
        return checked_metadata(metadata)

    def open_stream(self, name: str, mode: str):
        descriptor = os.open(name, STREAM_FLAGS[mode] | os.O_NOFOLLOW, 0o600, dir_fd=self._token)
        return os.fdopen(descriptor, STREAM_MODES[mode])

    def replace(self, source: str, target: str) -> None:
        os.replace(source, target, src_dir_fd=self._token, dst_dir_fd=self._token)

    def unlink(self, name: str) -> None:
        try:
            os.unlink(name, dir_fd=self._token)
        except FileNotFoundError:
            pass

    def sync(self) -> None:
        """Persist the replace metadata; a refused fsync is not a lost write."""

        try:
            os.fsync(self._token)
        except OSError:
            pass


def _dir_fd_supported() -> bool:
    operations = (os.open, os.mkdir, os.stat, os.replace, os.unlink)
    return (hasattr(os, 'O_DIRECTORY') and hasattr(os, 'O_NOFOLLOW') and
            all(operation in os.supports_dir_fd for operation in operations))


BOUND_DIRECTORY = _WindowsBinding if os.name == 'nt' else _PosixBinding
BINDING_SUPPORTED = os.name == 'nt' or _dir_fd_supported()


def _anchor_binding(anchor: Path):
    """Bind the StateRoot itself, or report that it is not there yet.

    A redirection at or above the StateRoot is the host's own decision, so the
    anchor is opened followed: what it names is where the registry lives, and
    every level below it is bound as itself.  A platform that can offer neither
    a pinning handle nor ``dir_fd`` is refused rather than told it is protected.
    """

    if not BINDING_SUPPORTED:
        refuse('path_binding_unsupported')
    if os.name == 'nt':
        handle = _open_handle(anchor, follow=True)
        if handle is None:
            return None
        if not _handle_attributes(handle) & DIRECTORY_ATTRIBUTE:
            KERNEL32.CloseHandle(handle)
            return None
        return BOUND_DIRECTORY(handle, Path(_handle_path(handle)))
    try:
        return BOUND_DIRECTORY(os.open(anchor, DIRECTORY_FLAGS), anchor)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as error:
        raise KnowledgeRegistryError('registry_unavailable') from error


@contextmanager
def bound_chain(anchor: Path, directory: Path, create: bool):
    """Hold every directory from the StateRoot down to ``directory`` open.

    Each component is first checked as given -- that is the evidence a planted
    link leaves behind, and one already there is refused on the spot -- and
    then bound, which is what actually closes the window: a bound directory
    can be neither renamed away nor replaced while the operation runs, so
    nothing below it can be redirected between the create, the lease open and
    the replace.  ``None`` reports that the chain of real directories stops
    short, which only a read tolerates.
    """

    if create:
        try:
            anchor.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise KnowledgeRegistryError('registry_unwritable') from error
    with ExitStack() as stack:
        bound = _anchor_binding(anchor)
        current = anchor
        for part in components(anchor, directory):
            if bound is None:
                break
            stack.callback(bound.close)
            current = current / part
            link_metadata(current)
            bound = bound.child(part, create)
        if bound is not None:
            stack.callback(bound.close)
            if create:
                _refuse_escape(anchor, directory)
        yield bound
