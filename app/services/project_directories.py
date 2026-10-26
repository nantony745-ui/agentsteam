"""参照 Codeg DirectoryBrowser，由服务端列举项目目录。"""

import os
from pathlib import Path


def list_project_directories(value: str | None) -> dict:
    path = Path(value) if value else Path.home()
    if not path.is_absolute():
        raise ValueError("目录路径必须是绝对路径")
    path = path.resolve(strict=True)
    if not path.is_dir():
        raise ValueError("请选择文件夹，而不是文件")
    directories = []
    with os.scandir(path) as entries:
        for entry in entries:
            if entry.is_dir():
                directories.append({"name": entry.name, "path": str(path / entry.name)})
    roots = [str(Path.home()), *os.listdrives()]
    return {
        "path": str(path),
        "parent": str(path.parent) if path != path.parent else None,
        "directories": sorted(directories, key=lambda item: item["name"].casefold()),
        "roots": list(dict.fromkeys(roots)),
    }
