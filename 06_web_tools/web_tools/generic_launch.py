"""General Win32 launch preparation; discovery is not a compatibility guarantee."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


class GenericLaunchError(ValueError):
    def __init__(self,code,message):
        super().__init__(message);self.code=code


def prepare_application(args:dict[str,Any])->dict:
    from .installed_apps import discover_installed_apps,validate_executable
    executable=args.get('executable')
    entry=None
    if executable is None:
        app=args.get('app')
        if not isinstance(app,str) or not app.strip():
            raise GenericLaunchError('application_required','Choose an installed app_id/name or an absolute executable path')
        if Path(app).is_absolute():executable=app
        else:
            entries=discover_installed_apps(limit=1000)
            found=[item for item in entries if item['app_id']==app or item['name'].casefold()==app.casefold()]
            if len(found)!=1:
                raise GenericLaunchError('application_selection_required','Use list_apps to select an unambiguous app_id, or supply its executable path')
            entry=found[0];executable=entry['executable']
    checked=validate_executable(executable)
    if checked is None:raise GenericLaunchError('application_executable_invalid','Choose an existing absolute GUI .exe, not a shell, protocol or maintenance launcher')
    path=Path(checked)
    argv=args.get('argv',entry.get('argv',[]) if entry else [])
    if not isinstance(argv,list) or len(argv)>100 or any(not isinstance(a,str) or len(a)>4000 or '\0' in a or '\r' in a or '\n' in a for a in argv):
        raise GenericLaunchError('invalid_application_args','Arguments must be a bounded array of literal strings')
    argv=list(argv)
    document=args.get('document_path')
    if document is not None:
        if not isinstance(document,str) or not Path(document).is_absolute():
            raise GenericLaunchError('document_path_required','Use an absolute existing local document path')
        document=Path(document).resolve(strict=True)
        if not document.is_file():raise GenericLaunchError('document_path_denied','Document is not a regular file')
        argv.append(str(document))
    cwd=args.get('cwd') or (entry.get('cwd') if entry else None) or str(path.parent)
    if not isinstance(cwd,str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
        raise GenericLaunchError('invalid_application_cwd','Application cwd must be an existing absolute directory')
    return {'executable':path,'argv':[str(path),*argv],'cwd':Path(cwd).resolve(),
            'name':entry['name'] if entry else path.stem,'source':entry['source'] if entry else 'explicit_executable',
            'background_compatibility':'unverified'}


def check_user_instance(executable:Path)->None:
    import psutil
    from .background_lifecycle import _default_window_pids
    default_pids=_default_window_pids()
    expected=str(executable.resolve()).casefold()
    for pid in default_pids:
        try:
            if str(Path(psutil.Process(pid).exe()).resolve()).casefold()==expected:
                raise GenericLaunchError('existing_user_instance_conflict',
                    'This executable already owns a user-desktop window. Its unknown single-instance behavior could redirect into that window; no background launch was attempted')
        except psutil.Error:
            # A process that exits during enumeration cannot receive activation.
            continue


def ui_restrictions()->int:
    import win32job
    return (win32job.JOB_OBJECT_UILIMIT_HANDLES|win32job.JOB_OBJECT_UILIMIT_READCLIPBOARD|
            win32job.JOB_OBJECT_UILIMIT_WRITECLIPBOARD|win32job.JOB_OBJECT_UILIMIT_SYSTEMPARAMETERS|
            win32job.JOB_OBJECT_UILIMIT_DISPLAYSETTINGS|win32job.JOB_OBJECT_UILIMIT_DESKTOP|
            win32job.JOB_OBJECT_UILIMIT_EXITWINDOWS)
