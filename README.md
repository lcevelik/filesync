# FileSync - Precision File Synchronization Tool

A modern, intelligent file synchronization tool with special support for Unreal Engine projects. Built with PyQt6 featuring a beautiful dark theme UI and smart project detection.

![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-blue)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## ✨ Features

### 🎯 **Smart Unreal Engine Detection**
- Automatically detects UE projects (`.uproject` files)
- Intelligent labeling: "Source (Editor / Control)" → "Destination (Node 0, 1, 2...)"
- Auto-excludes build artifacts:
  - `Intermediate/`
  - `Saved/`
  - `DerivedDataCache/`
  - `Binaries/`
  - `.git/`, `.vs/`, `.vscode/`
- Dramatically faster syncing for UE projects

### 🚀 **Advanced Sync Engine**
- **SHA-256 hash verification** for file integrity
- **Parallel processing** - leverages all CPU cores
- **Multi-destination support** - sync to multiple locations simultaneously
- **Smart file comparison** - only syncs what's needed
- **Cancelable operations** - stop long-running tasks anytime

### 💎 **Modern UI/UX**
- **Dark theme** - easy on the eyes
- **Drag & drop support** - drop folders directly into fields
- **Qt file browser** - shows files AND folders (unlike native Windows picker)
- **Real-time progress** - see exactly what's happening
- **Persistent settings** - paths remembered between sessions
- **Status filtering** - view New, Modified, Unchanged, or Dest-only files

### 🎨 **User Interface**
- Clean, professional PyQt6 interface
- Real-time file status with color coding:
  - 🟢 **Green** - New files
  - 🟡 **Yellow** - Modified files
  - 🔴 **Red** - Destination-only files
  - ⚪ **Gray** - Unchanged files
- Detail view with file sizes and sync targets
- Comprehensive logging of all operations

## 📥 Installation

### Option 1: Download Executable (Recommended)
1. Download `FileSync.exe` from the [Releases](../../releases) page
2. Run it - no installation required!
3. Settings are automatically saved to `~/.filesync_settings.json`

### Option 2: Run from Source
```bash
# Clone the repository
git clone https://github.com/lcevelik/filesync.git
cd filesync

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run the application
python filesync_qt.py
```

## 🎮 Usage

### Quick Start
1. **Set Source** - Browse or drag & drop your source folder
2. **Set Destination(s)** - Add one or more destination folders
3. **Scan** - Click "Scan / Compare" to analyze differences
4. **Review** - Check the file list to see what will sync
5. **Sync** - Click "Sync →" to copy files

### For Unreal Engine Projects
1. Drop your UE project folder into the Source field
2. App automatically detects `.uproject` file
3. Labels change to "Editor / Control" and "Node 0, 1, 2..."
4. Build artifacts are automatically excluded
5. Sync only what matters - much faster!

### Multi-Destination Sync
- Click **"+ Add Destination"** to add more sync targets
- Each file only copies to destinations that need it
- Perfect for syncing to multiple build machines or backup locations

### Keyboard Shortcuts
- **Drag & Drop** - Drag folders from Explorer/Finder into any path field
- **Browse** - Click Browse button for file dialog with file visibility
- **Filter** - Type in the filter box to search files
- **Cancel** - Stop any long-running operation

## 🛠️ Building from Source

### Requirements
- Python 3.9 or higher
- PyQt6 6.10+
- PyInstaller 6.0+ (for building executable)

### Build Executable
```bash
# Install build dependencies
pip install -r requirements.txt

# Build single-file executable
pyinstaller --onefile --windowed --name "FileSync" filesync_qt.py --clean

# Find executable in dist/FileSync.exe
```

## 📋 Requirements

### Python Dependencies
- **PyQt6** - Modern GUI framework
- **xxhash** (optional) - Faster hashing (3-5x speedup)

See `requirements.txt` for complete list.

### System Requirements
- **Windows 10+** / **macOS 10.14+** / **Linux**
- 4GB RAM minimum
- Multi-core CPU recommended for parallel hashing

## ⚙️ Configuration

Settings are automatically saved to:
```
~/.filesync_settings.json
```

Contains:
- Source path
- Destination paths
- Window preferences

Delete this file to reset to defaults.

## 🎯 Use Cases

### Unreal Engine Development
- Sync project between editor machine and build nodes
- Backup projects efficiently (skip build artifacts)
- Share projects with team members
- Deploy to multiple test environments

### General File Sync
- Backup important folders
- Mirror directories across drives
- Sync to multiple locations simultaneously
- Verify file integrity with SHA-256

### Network Sync
- Copy to network shares
- Deploy to multiple servers
- Maintain synchronized backup locations

## 🔧 Technical Details

### Sync Algorithm
1. **Phase 1**: Parallel directory scanning
2. **Phase 2**: Size comparison to identify candidates
3. **Phase 3**: SHA-256 hashing (parallel) for verification
4. **Phase 4**: Multi-threaded file copying

### Exclusion Patterns (UE Projects)
Automatically excluded when UE project detected:
- `Intermediate/` - Temporary build files
- `Saved/` - Logs, configs, autosaves
- `DerivedDataCache/` - Cached derived data
- `Binaries/` - Compiled binaries
- `.git/`, `.vs/`, `.vscode/` - Development artifacts

### Performance
- Utilizes all CPU cores for hashing
- Up to 32 parallel worker threads
- Smart caching to avoid redundant operations
- Efficient memory usage even for large projects

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## 📝 License

This project is licensed under the MIT License - see the LICENSE file for details.

## 🐛 Bug Reports

Found a bug? Please open an issue on [GitHub Issues](../../issues).

## 💡 Feature Requests

Have an idea? Open an issue with the "enhancement" label!

## 🙏 Acknowledgments

- Built with [PyQt6](https://www.riverbankcomputing.com/software/pyqt/)
- Inspired by the needs of Unreal Engine developers
- Dark theme inspired by macOS Big Sur and Visual Studio Code

## 📞 Support

- **Issues**: [GitHub Issues](../../issues)
- **Discussions**: [GitHub Discussions](../../discussions)

---

**Made with ❤️ for Unreal Engine developers and anyone who needs reliable file synchronization**

Co-Authored-By: Claude Sonnet 4.5
