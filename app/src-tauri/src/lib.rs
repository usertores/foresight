use std::fs::{self, File};
use std::net::TcpStream;
#[cfg(windows)]
use std::os::windows::io::AsRawHandle;
#[cfg(windows)]
use std::os::windows::process::CommandExt;
#[cfg(unix)]
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, Manager, RunEvent, WindowEvent};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};
#[cfg(windows)]
use win32job::{ExtendedLimitInfo, Job};

const API_ADDR: &str = "127.0.0.1:8765";
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

#[cfg(target_os = "macos")]
const OPEN_LABEL: &str = "Open   (⌘⇧Space)";
#[cfg(not(target_os = "macos"))]
const OPEN_LABEL: &str = "Open   (Ctrl+Shift+Space)";

/// The Python API process. On Windows a job object ties its lifetime (and the
/// llama.cpp servers it spawns) to this app: when the app exits, even by
/// crashing, Windows closes the job and kills the whole process tree. On macOS
/// the backend leads its own process group, which is stopped on exit; if the app
/// crashes, the backend sees its parent is gone and quits (FORESIGHT_PARENT_PID).
struct Backend {
    child: Mutex<Option<Child>>,
    #[cfg(windows)]
    _job: Option<Job>,
}

impl Backend {
    fn none() -> Self {
        Backend {
            child: Mutex::new(None),
            #[cfg(windows)]
            _job: None,
        }
    }
}

fn project_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..").join("..")
}

#[cfg(windows)]
fn python(backend: &Path) -> PathBuf {
    backend.join(".venv").join("Scripts").join("python.exe")
}

#[cfg(not(windows))]
fn python(backend: &Path) -> PathBuf {
    backend.join(".venv").join("bin").join("python")
}

fn spawn_backend() -> Backend {
    // Already running (e.g. started by hand while developing): reuse it.
    let addr = API_ADDR.parse().unwrap();
    if TcpStream::connect_timeout(&addr, Duration::from_millis(300)).is_ok() {
        return Backend::none();
    }

    let root = project_root();
    let backend = root.join("backend");
    let logs = root.join("data").join("logs");
    let _ = fs::create_dir_all(&logs);
    let log = File::create(logs.join("api.log")).ok();

    let mut cmd = Command::new(python(&backend));
    cmd.args(["-m", "foresight", "serve"])
        .current_dir(&backend)
        .env("PYTHONUNBUFFERED", "1");
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    #[cfg(unix)]
    cmd.process_group(0)
        .env("FORESIGHT_PARENT_PID", std::process::id().to_string());
    match log {
        Some(f) => {
            cmd.stdout(f.try_clone().map(Stdio::from).unwrap_or(Stdio::null()));
            cmd.stderr(Stdio::from(f));
        }
        None => {
            cmd.stdout(Stdio::null()).stderr(Stdio::null());
        }
    }

    let child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("could not start backend: {e}");
            return Backend::none();
        }
    };

    #[cfg(windows)]
    let job = Job::create().ok().and_then(|job| {
        let mut info = ExtendedLimitInfo::new();
        info.limit_kill_on_job_close();
        job.set_extended_limit_info(&info).ok()?;
        job.assign_process(child.as_raw_handle() as isize).ok()?;
        Some(job)
    });

    Backend {
        child: Mutex::new(Some(child)),
        #[cfg(windows)]
        _job: job,
    }
}

#[cfg(windows)]
fn stop_backend(child: &mut Child) {
    let _ = child.kill();
}

#[cfg(unix)]
fn stop_backend(child: &mut Child) {
    // SIGTERM lets it close a class session on the PC; the group (the backend and the
    // audio helper it runs) is killed if it is still there after 2 s.
    let group = child.id() as libc::pid_t;
    unsafe { libc::killpg(group, libc::SIGTERM) };
    for _ in 0..20 {
        if matches!(child.try_wait(), Ok(Some(_))) {
            break;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    unsafe { libc::killpg(group, libc::SIGKILL) };
    let _ = child.wait();
}

fn show(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
        let _ = w.emit("focus-search", ());
    }
}

fn toggle(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        if w.is_visible().unwrap_or(false) && w.is_focused().unwrap_or(false) {
            let _ = w.hide();
        } else {
            show(app);
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    #[cfg(target_os = "macos")]
    let shortcut = Shortcut::new(Some(Modifiers::SUPER | Modifiers::SHIFT), Code::Space);
    #[cfg(not(target_os = "macos"))]
    let shortcut = Shortcut::new(Some(Modifiers::CONTROL | Modifiers::SHIFT), Code::Space);

    let app = tauri::Builder::default()
        // Must be first: a second launch just focuses the running window.
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| show(app)))
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(move |app, sc, event| {
                    if sc == &shortcut && event.state() == ShortcutState::Pressed {
                        toggle(app);
                    }
                })
                .build(),
        )
        .setup(move |app| {
            app.manage(spawn_backend());
            app.global_shortcut().register(shortcut)?;

            let open_i = MenuItem::with_id(app, "open", OPEN_LABEL, true, None::<&str>)?;
            let quit_i = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&open_i, &quit_i])?;
            TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("Foresight")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, e| match e.id.as_ref() {
                    "open" => show(app),
                    "quit" => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        show(tray.app_handle());
                    }
                })
                .build(app)?;
            Ok(())
        })
        // Closing the window hides it; the app keeps running in the tray so the
        // shortcut stays instant. "Quit" in the tray menu quits for real.
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|app, event| match event {
        RunEvent::Exit => {
            if let Some(backend) = app.try_state::<Backend>() {
                if let Some(mut child) = backend.child.lock().unwrap().take() {
                    stop_backend(&mut child);
                }
            }
        }
        // Clicking the Dock icon brings back the hidden window.
        #[cfg(target_os = "macos")]
        RunEvent::Reopen { .. } => show(app),
        _ => {}
    });
}
