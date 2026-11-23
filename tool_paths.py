"""Locate the external programs the pipeline drives (override with environment variables)."""
import glob, os, shutil


def _first(cands):
    for c in cands:
        for hit in sorted(glob.glob(c), reverse=True):   # newest version folder first
            if os.path.isfile(hit):
                return hit
    return None


def freecadcmd():
    """FreeCAD console executable: $FREECADCMD, PATH, or the usual install folders."""
    env = os.environ.get('FREECADCMD')
    if env:
        return env
    on_path = shutil.which('freecadcmd') or shutil.which('FreeCADCmd')
    if on_path:
        return on_path
    roots = [os.environ.get('LOCALAPPDATA', ''), os.environ.get('ProgramFiles', r'C:\Program Files')]
    return _first([os.path.join(r, 'Programs', 'FreeCAD*', 'bin', 'freecadcmd.exe') for r in roots] +
                  [os.path.join(r, 'FreeCAD*', 'bin', 'freecadcmd.exe') for r in roots] +
                  ['/usr/bin/freecadcmd', '/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd']) or 'freecadcmd'


def solidcraft_cli():
    """SolidCraft command-line tool: $SOLIDCRAFT_CLI, PATH, or the usual install folder."""
    env = os.environ.get('SOLIDCRAFT_CLI')
    if env:
        return env
    on_path = shutil.which('solidcraft-cli')
    if on_path:
        return on_path
    return _first([os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'), 'SolidCraft', 'solidcraft-cli.exe'),
                   os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', 'SolidCraft', 'solidcraft-cli.exe')]) \
        or 'solidcraft-cli'
