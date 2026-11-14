//! peer-hold — an agent's terminal, held without a parent.
//!
//! tmux did two jobs for agents: keep the PTY alive after the viewer left, and draw
//! windows/panes. The second job now belongs to sm, so this keeps only the first.
//! One small process per agent drains the PTY, models its visible screen, and lets
//! viewers attach over a Unix socket. Live bytes pass through unchanged; reconnects
//! receive one synchronized screen snapshot, never historical terminal commands.

mod screen;

use std::collections::VecDeque;
use std::ffi::CString;
use std::io::{self, Read, Write};
use std::os::unix::io::{AsRawFd, RawFd};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const T_HELLO: u8 = 1; // client → holder: cols u16, rows u16
const T_INPUT: u8 = 2; // client → holder: bytes for the PTY
const T_RESIZE: u8 = 3; // client → holder: cols u16, rows u16
const T_STATUS: u8 = 4; // client → holder: ask for status JSON
const T_OUTPUT: u8 = 10; // holder → client: PTY bytes
const T_EXIT: u8 = 11; // holder → client: i32 exit code
const T_STATUS_R: u8 = 12; // holder → client: status JSON
const T_TAIL: u8 = 5; // client → holder: ask for the replay ring
const T_TAIL_R: u8 = 13; // holder → client: ring bytes

const MAX_CLIENT_BACKLOG: usize = 8 << 20; // a viewer this far behind is dropped, the agent never waits
const SCREEN_MAGIC: &[u8] = b"PHSC\x01"; // followed by cols/rows u16 BE, then frozen ANSI snapshot

fn usage() -> ! {
    eprintln!(
        "peer-hold — keep an agent's terminal alive without a parent, attach to it from anywhere

  peer-hold start --sock PATH [--cwd DIR] [--env K=V]... [--cols C --rows R] [--ring BYTES] -- CMD [ARGS...]
      Start CMD on a new PTY under a detached holder. Returns once the socket is live and
      prints {{\"holder\":PID,\"child\":PID,\"sock\":PATH}}. PATH.json describes it; PATH.exit
      holds the exit code after CMD ends. CMD sees PEER_HOLD_SOCK=PATH; front-end ids
      (FINE_TAB_ID, SM_PANE_ID, TMUX_PANE) are not inherited — pass them with --env.
  peer-hold attach --sock PATH [--detach-key] [--passive]
      Attach this terminal. Closing the terminal detaches; the agent keeps running.
      The agent's size follows the viewer that last typed; --passive never sets it (side views).
      --detach-key: Ctrl-\\ detaches too (off by default so agents keep that key).
  peer-hold tail --sock PATH [--lines N] [--raw]
      Recent output without attaching. Default strips escape sequences into text lines,
      good for grepping errors or [REPORT]; this log can repeat TUI redraws.
  peer-hold status --sock PATH
      Print the holder's status JSON (exit 1 if nothing is listening).
  peer-hold screen --file PATH [--cols C --rows R]
      Read a saved .screen as one safe screen. Legacy raw logs are converted using
      the supplied dimensions (default 120x40); historical queries are not replayed."
    );
    std::process::exit(2)
}

fn die(msg: impl std::fmt::Display) -> ! {
    eprintln!("peer-hold: {msg}");
    std::process::exit(1)
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let Some(cmd) = args.first() else { usage() };
    let rest = &args[1..];
    match cmd.as_str() {
        "start" => start(rest),
        "attach" => attach(rest),
        "status" => status(rest),
        "tail" => tail(rest),
        "screen" => saved_screen(rest),
        "-h" | "--help" | "help" => usage(),
        _ => usage(),
    }
}

struct Opts {
    sock: Option<PathBuf>,
    file: Option<PathBuf>,
    cwd: Option<PathBuf>,
    cols: u16,
    rows: u16,
    ring: usize,
    detach_key: bool,
    passive: bool,
    lines: usize,
    raw: bool,
    env: Vec<(String, String)>,
    command: Vec<String>,
}

fn parse(rest: &[String]) -> Opts {
    let mut o = Opts { sock: None, file: None, cwd: None, cols: 120, rows: 40, ring: 1 << 20, detach_key: false, passive: false, lines: 40, raw: false, env: vec![], command: vec![] };
    let mut i = 0;
    let val = |i: usize| rest.get(i + 1).cloned().unwrap_or_else(|| usage());
    while i < rest.len() {
        match rest[i].as_str() {
            "--sock" => { o.sock = Some(PathBuf::from(val(i))); i += 2 }
            "--file" => { o.file = Some(PathBuf::from(val(i))); i += 2 }
            "--cwd" => { o.cwd = Some(PathBuf::from(val(i))); i += 2 }
            "--cols" => { o.cols = val(i).parse().unwrap_or_else(|_| usage()); i += 2 }
            "--rows" => { o.rows = val(i).parse().unwrap_or_else(|_| usage()); i += 2 }
            "--ring" => { o.ring = val(i).parse().unwrap_or_else(|_| usage()); i += 2 }
            "--detach-key" => { o.detach_key = true; i += 1 }
            "--passive" => { o.passive = true; i += 1 }
            "--lines" => { o.lines = val(i).parse().unwrap_or_else(|_| usage()); i += 2 }
            "--raw" => { o.raw = true; i += 1 }
            "--env" => {
                let kv = val(i);
                let (k, v) = kv.split_once('=').unwrap_or_else(|| usage());
                o.env.push((k.to_string(), v.to_string()));
                i += 2
            }
            "--" => { o.command = rest[i + 1..].to_vec(); break }
            _ => usage(),
        }
    }
    o
}

// ---------- framing ----------

fn frame(t: u8, payload: &[u8]) -> Vec<u8> {
    let mut v = Vec::with_capacity(5 + payload.len());
    v.push(t);
    v.extend_from_slice(&(payload.len() as u32).to_be_bytes());
    v.extend_from_slice(payload);
    v
}

/// Pop one complete frame from the front of `buf`.
fn take_frame(buf: &mut Vec<u8>) -> Option<(u8, Vec<u8>)> {
    if buf.len() < 5 {
        return None;
    }
    let len = u32::from_be_bytes([buf[1], buf[2], buf[3], buf[4]]) as usize;
    if buf.len() < 5 + len {
        return None;
    }
    let t = buf[0];
    let payload = buf[5..5 + len].to_vec();
    buf.drain(..5 + len);
    Some((t, payload))
}

fn size_payload(cols: u16, rows: u16) -> [u8; 4] {
    let c = cols.to_be_bytes();
    let r = rows.to_be_bytes();
    [c[0], c[1], r[0], r[1]]
}

fn read_size(p: &[u8]) -> Option<(u16, u16)> {
    if p.len() < 4 {
        return None;
    }
    let cols = u16::from_be_bytes([p[0], p[1]]);
    let rows = u16::from_be_bytes([p[2], p[3]]);
    (cols > 0 && rows > 0).then_some((cols, rows))
}

// ---------- small libc helpers ----------

fn set_nonblock(fd: RawFd) {
    unsafe {
        let fl = libc::fcntl(fd, libc::F_GETFL);
        libc::fcntl(fd, libc::F_SETFL, fl | libc::O_NONBLOCK);
    }
}

fn set_cloexec(fd: RawFd) {
    unsafe {
        libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC);
    }
}

fn set_winsize(fd: RawFd, cols: u16, rows: u16) {
    let ws = libc::winsize { ws_row: rows, ws_col: cols, ws_xpixel: 0, ws_ypixel: 0 };
    unsafe {
        libc::ioctl(fd, libc::TIOCSWINSZ, &ws);
    }
}

fn get_winsize(fd: RawFd) -> Option<(u16, u16)> {
    let mut ws: libc::winsize = unsafe { std::mem::zeroed() };
    let ok = unsafe { libc::ioctl(fd, libc::TIOCGWINSZ, &mut ws) } == 0;
    (ok && ws.ws_col > 0 && ws.ws_row > 0).then_some((ws.ws_col, ws.ws_row))
}

fn raw_write_all(fd: RawFd, mut data: &[u8]) {
    while !data.is_empty() {
        let n = unsafe { libc::write(fd, data.as_ptr() as *const _, data.len()) };
        if n < 0 {
            let e = io::Error::last_os_error();
            if e.kind() == io::ErrorKind::Interrupted {
                continue;
            }
            if e.kind() == io::ErrorKind::WouldBlock {
                let mut p = libc::pollfd { fd, events: libc::POLLOUT, revents: 0 };
                unsafe { libc::poll(&mut p, 1, 1000) };
                continue;
            }
            return;
        }
        data = &data[n as usize..];
    }
}

fn now_secs() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

fn json_str(s: &str) -> String {
    let mut o = String::with_capacity(s.len() + 2);
    o.push('"');
    for c in s.chars() {
        match c {
            '"' => o.push_str("\\\""),
            '\\' => o.push_str("\\\\"),
            '\n' => o.push_str("\\n"),
            '\r' => o.push_str("\\r"),
            '\t' => o.push_str("\\t"),
            c if (c as u32) < 0x20 => o.push_str(&format!("\\u{:04x}", c as u32)),
            c => o.push(c),
        }
    }
    o.push('"');
    o
}

// Self-pipe for signals so poll() wakes on SIGCHLD / SIGWINCH.
static mut SIG_PIPE_W: RawFd = -1;
extern "C" fn on_signal(_: libc::c_int) {
    unsafe {
        let b = 1u8;
        libc::write(SIG_PIPE_W, &b as *const u8 as *const _, 1);
    }
}
fn signal_pipe(signals: &[libc::c_int]) -> RawFd {
    let mut fds = [0; 2];
    unsafe {
        libc::pipe(fds.as_mut_ptr());
        set_nonblock(fds[0]);
        set_nonblock(fds[1]);
        set_cloexec(fds[0]);
        set_cloexec(fds[1]);
        SIG_PIPE_W = fds[1];
        for &s in signals {
            libc::signal(s, on_signal as extern "C" fn(libc::c_int) as libc::sighandler_t);
        }
    }
    fds[0]
}
fn drain_fd(fd: RawFd) {
    let mut b = [0u8; 64];
    while unsafe { libc::read(fd, b.as_mut_ptr() as *mut _, b.len()) } > 0 {}
}

// ---------- start / holder ----------

fn start(rest: &[String]) {
    let o = parse(rest);
    let sock = o.sock.clone().unwrap_or_else(|| usage());
    if o.command.is_empty() {
        usage();
    }
    if let Some(dir) = sock.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    if UnixStream::connect(&sock).is_ok() {
        die(format!("{} is already held by a live holder", sock.display()));
    }
    let _ = std::fs::remove_file(&sock);
    let _ = std::fs::remove_file(sock.with_extension("exit"));
    let listener = UnixListener::bind(&sock).unwrap_or_else(|e| die(format!("bind {}: {e}", sock.display())));
    unsafe { libc::chmod(CString::new(sock.as_os_str().as_encoded_bytes()).unwrap().as_ptr(), 0o600) };
    set_cloexec(listener.as_raw_fd());

    let cwd = o.cwd.clone().unwrap_or_else(|| std::env::current_dir().unwrap_or_else(|_| PathBuf::from("/")));
    let c_args: Vec<CString> = o.command.iter().map(|a| CString::new(a.as_str()).unwrap_or_else(|_| die("NUL in argument"))).collect();
    let c_cwd = CString::new(cwd.as_os_str().as_encoded_bytes()).unwrap_or_else(|_| die("NUL in cwd"));

    let mut ready = [0; 2];
    unsafe { libc::pipe(ready.as_mut_ptr()) };
    set_cloexec(ready[0]);
    set_cloexec(ready[1]);

    match unsafe { libc::fork() } {
        -1 => die("fork failed"),
        0 => {
            // Holder. New session: no controlling terminal, no parent to take it down.
            unsafe {
                libc::close(ready[0]);
                libc::setsid();
                let null = libc::open(c"/dev/null".as_ptr(), libc::O_RDWR);
                libc::dup2(null, 0);
                libc::dup2(null, 1);
                libc::dup2(null, 2);
                if null > 2 { libc::close(null); }
                libc::signal(libc::SIGPIPE, libc::SIG_IGN);
                libc::signal(libc::SIGHUP, libc::SIG_IGN);
            }
            holder(o, sock, cwd, listener, c_args, c_cwd, ready[1]);
        }
        _ => {
            unsafe { libc::close(ready[1]) };
            drop(listener);
            let mut buf = [0u8; 64];
            let n = unsafe { libc::read(ready[0], buf.as_mut_ptr() as *mut _, buf.len()) };
            if n <= 0 {
                die("holder did not start");
            }
            let text = String::from_utf8_lossy(&buf[..n as usize]).to_string();
            let mut parts = text.split_whitespace();
            let holder_pid = parts.next().unwrap_or("0");
            let child_pid = parts.next().unwrap_or("0");
            if child_pid == "0" {
                die("could not start the command on a PTY");
            }
            println!(
                "{{\"holder\":{},\"child\":{},\"sock\":{}}}",
                holder_pid, child_pid, json_str(&sock.to_string_lossy())
            );
        }
    }
}

struct Client {
    id: u64,
    stream: UnixStream,
    inbuf: Vec<u8>,
    out: VecDeque<u8>,
    attached: bool,
    size: Option<(u16, u16)>, // None: passive viewer (never sets the PTY size)
}

fn holder(o: Opts, sock: PathBuf, cwd: PathBuf, listener: UnixListener, c_args: Vec<CString>, c_cwd: CString, ready_w: RawFd) -> ! {
    let mut master: RawFd = -1;
    let mut slave: RawFd = -1;
    let mut ws = libc::winsize { ws_row: o.rows, ws_col: o.cols, ws_xpixel: 0, ws_ypixel: 0 };
    let ok = unsafe { libc::openpty(&mut master, &mut slave, std::ptr::null_mut(), std::ptr::null_mut(), &mut ws) } == 0;
    if !ok {
        unsafe { libc::write(ready_w, b"0 0".as_ptr() as *const _, 3) };
        std::process::exit(1);
    }
    set_cloexec(master);
    if std::env::var_os("TERM").map_or(true, |t| t.is_empty() || t == "dumb") {
        std::env::set_var("TERM", "xterm-256color");
    }
    // The agent's identity is its holder, not whichever front end ran `start`: inherited
    // pane ids would make peer file it under the starter's Fine tab / sm pane / tmux pane.
    for k in ["TMUX", "TMUX_PANE", "FINE_TAB_ID", "SM_PANE_ID"] {
        std::env::remove_var(k);
    }
    for (k, v) in &o.env {
        std::env::set_var(k, v);
    }
    std::env::set_var("PEER_HOLD_SOCK", &sock);

    let mut argv: Vec<*const libc::c_char> = c_args.iter().map(|a| a.as_ptr()).collect();
    argv.push(std::ptr::null());
    let sig_r = signal_pipe(&[libc::SIGCHLD]);

    let child = unsafe { libc::fork() };
    if child == 0 {
        unsafe {
            libc::setsid();
            libc::ioctl(slave, libc::TIOCSCTTY as _, 0);
            libc::dup2(slave, 0);
            libc::dup2(slave, 1);
            libc::dup2(slave, 2);
            if slave > 2 { libc::close(slave); }
            libc::signal(libc::SIGPIPE, libc::SIG_DFL);
            libc::signal(libc::SIGHUP, libc::SIG_DFL);
            libc::signal(libc::SIGCHLD, libc::SIG_DFL);
            libc::chdir(c_cwd.as_ptr());
            libc::execvp(argv[0], argv.as_ptr());
            libc::_exit(127);
        }
    }
    unsafe { libc::close(slave) };
    let msg = format!("{} {}", std::process::id(), if child > 0 { child } else { 0 });
    unsafe {
        libc::write(ready_w, msg.as_ptr() as *const _, msg.len());
        libc::close(ready_w);
    }
    if child <= 0 {
        std::process::exit(1);
    }
    set_nonblock(master);
    listener.set_nonblocking(true).ok();

    let started = now_secs();
    let meta = format!(
        "{{\"holder\":{},\"child\":{},\"sock\":{},\"cwd\":{},\"cmd\":[{}],\"env\":[{}],\"started\":{:.3}}}\n",
        std::process::id(),
        child,
        json_str(&sock.to_string_lossy()),
        json_str(&cwd.to_string_lossy()),
        o.command.iter().map(|a| json_str(a)).collect::<Vec<_>>().join(","),
        // What --env set, so a restore after reboot can start the same session the same way.
        o.env.iter().map(|(k, v)| json_str(&format!("{k}={v}"))).collect::<Vec<_>>().join(","),
        started
    );
    let _ = std::fs::write(sock.with_extension("json"), meta);

    let mut ring: VecDeque<u8> = VecDeque::with_capacity(o.ring.min(1 << 16));
    let mut screen = screen::Screen::new(o.cols, o.rows);
    let mut clients: Vec<Client> = Vec::new();
    let (mut cols, mut rows) = (o.cols, o.rows);
    // Several viewers can show one agent at different sizes; the PTY can have only one. It follows the
    // viewer whose hands are on it: the last one to send input (or, before any input, the last active
    // viewer to attach). Passive viewers (a leftover tmux pane, a preview) never change it.
    let mut owner: Option<u64> = None;
    let mut last_seen: f64 = 0.0;
    let mut next_id: u64 = 0;
    let mut exit_code: Option<i32> = None;
    let mut buf = vec![0u8; 64 * 1024];

    loop {
        // Reap the child; once it's gone, drain what's left and say so.
        if exit_code.is_none() {
            let mut st = 0;
            let r = unsafe { libc::waitpid(child, &mut st, libc::WNOHANG) };
            if r == child {
                exit_code = Some(if libc::WIFEXITED(st) { libc::WEXITSTATUS(st) } else { 128 + libc::WTERMSIG(st) });
            }
        }
        if let Some(code) = exit_code {
            loop {
                let n = unsafe { libc::read(master, buf.as_mut_ptr() as *mut _, buf.len()) };
                if n <= 0 { break }
                screen.feed(&buf[..n as usize]);
                for c in clients.iter_mut().filter(|c| c.attached) {
                    c.out.extend(frame(T_OUTPUT, &buf[..n as usize]));
                }
            }
            // The last screen outlives the agent, so a viewer opened later (after an app restart) shows what
            // was there instead of a blank pane until the session is woken.
            let mut saved = SCREEN_MAGIC.to_vec();
            saved.extend_from_slice(&size_payload(cols, rows));
            saved.extend(screen.frozen());
            let _ = std::fs::write(sock.with_extension("screen"), saved);
            let _ = std::fs::write(sock.with_extension("exit"), format!("{code}\n"));
            let _ = std::fs::remove_file(&sock);
            let _ = std::fs::remove_file(sock.with_extension("json"));
            for c in clients.iter_mut() {
                c.out.extend(frame(T_EXIT, &code.to_be_bytes()));
                c.stream.set_nonblocking(false).ok();
                c.stream.set_write_timeout(Some(Duration::from_secs(2))).ok();
                let (a, b) = c.out.as_slices();
                let _ = c.stream.write_all(a);
                let _ = c.stream.write_all(b);
            }
            std::process::exit(0);
        }

        let mut pfds = vec![
            libc::pollfd { fd: master, events: libc::POLLIN, revents: 0 },
            libc::pollfd { fd: listener.as_raw_fd(), events: libc::POLLIN, revents: 0 },
            libc::pollfd { fd: sig_r, events: libc::POLLIN, revents: 0 },
        ];
        for c in &clients {
            let ev = libc::POLLIN | if c.out.is_empty() { 0 } else { libc::POLLOUT };
            pfds.push(libc::pollfd { fd: c.stream.as_raw_fd(), events: ev, revents: 0 });
        }
        let r = unsafe { libc::poll(pfds.as_mut_ptr(), pfds.len() as _, -1) };
        if r < 0 && io::Error::last_os_error().kind() != io::ErrorKind::Interrupted {
            std::process::exit(1);
        }

        if pfds[2].revents & libc::POLLIN != 0 {
            drain_fd(sig_r);
        }

        // Always drain the PTY, viewer or not: an undrained TUI stalls.
        if pfds[0].revents & (libc::POLLIN | libc::POLLHUP | libc::POLLERR) != 0 {
            let n = unsafe { libc::read(master, buf.as_mut_ptr() as *mut _, buf.len()) };
            if n > 0 {
                let data = &buf[..n as usize];
                screen.feed(data);
                ring.extend(data);
                if ring.len() > o.ring {
                    let extra = ring.len() - o.ring;
                    ring.drain(..extra);
                }
                for c in clients.iter_mut().filter(|c| c.attached) {
                    c.out.extend(frame(T_OUTPUT, data));
                }
            } else if n == 0 || io::Error::last_os_error().kind() != io::ErrorKind::WouldBlock {
                // Slave side closed (child gone). Wait for waitpid to report the code.
                let mut st = 0;
                let r = unsafe { libc::waitpid(child, &mut st, 0) };
                exit_code = Some(if r == child && libc::WIFEXITED(st) { libc::WEXITSTATUS(st) } else if r == child { 128 + libc::WTERMSIG(st) } else { 1 });
                continue;
            }
        }

        if pfds[1].revents & libc::POLLIN != 0 {
            while let Ok((s, _)) = listener.accept() {
                s.set_nonblocking(true).ok();
                set_cloexec(s.as_raw_fd());
                next_id += 1;
                clients.push(Client { id: next_id, stream: s, inbuf: Vec::new(), out: VecDeque::new(), attached: false, size: None });
            }
        }

        let viewers = clients.iter().filter(|c| c.attached).count();
        // Viewers that set the size are someone's screen (an sm or Fine pane); passive ones (a leftover tmux
        // viewer) are not. sm keeps a workspace awake while anyone actually looks at it.
        let active = clients.iter().filter(|c| c.attached && c.size.is_some()).count();
        if active > 0 {
            last_seen = now_secs();  // someone looked at it: sm counts idle time from here, not from an older turn
        }
        let mut dead = vec![false; clients.len()];
        for (i, c) in clients.iter_mut().enumerate() {
            let rev = pfds.get(3 + i).map_or(0, |p| p.revents); // clients accepted this round have no pollfd yet
            if rev & (libc::POLLIN | libc::POLLHUP | libc::POLLERR) != 0 {
                let mut tmp = [0u8; 16 * 1024];
                match c.stream.read(&mut tmp) {
                    Ok(0) => { dead[i] = true; continue }
                    Ok(n) => c.inbuf.extend_from_slice(&tmp[..n]),
                    Err(e) if e.kind() == io::ErrorKind::WouldBlock => {}
                    Err(_) => { dead[i] = true; continue }
                }
                while let Some((t, p)) = take_frame(&mut c.inbuf) {
                    match t {
                        T_HELLO => {
                            c.size = read_size(&p);
                            let (nc, nr) = match c.size { Some(sz) => { owner = Some(c.id); sz } None => (cols, rows) };
                            // A same-size attach does not resize or signal the agent.
                            if (nc, nr) != (cols, rows) {
                                cols = nc;
                                rows = nr;
                                screen.resize(cols, rows);
                                set_winsize(master, cols, rows);
                            }
                            c.out.extend(frame(T_OUTPUT, &screen.snapshot()));
                            c.attached = true;
                        }
                        T_INPUT => {
                            if owner != Some(c.id) {
                                if let Some((nc, nr)) = c.size {
                                    owner = Some(c.id);
                                    if (nc, nr) != (cols, rows) {
                                        cols = nc;
                                        rows = nr;
                                        screen.resize(cols, rows);
                                        set_winsize(master, cols, rows);
                                    }
                                }
                            }
                            raw_write_all(master, &p)
                        }
                        T_RESIZE => {
                            if let Some((nc, nr)) = read_size(&p) {
                                c.size = Some((nc, nr));
                                if owner == Some(c.id) && (nc, nr) != (cols, rows) {
                                    cols = nc;
                                    rows = nr;
                                    screen.resize(cols, rows);
                                    set_winsize(master, cols, rows);
                                }
                            }
                        }
                        T_TAIL => {
                            let (a, b) = ring.as_slices();
                            let mut all = Vec::with_capacity(ring.len());
                            all.extend_from_slice(a);
                            all.extend_from_slice(b);
                            c.out.extend(frame(T_TAIL_R, &all));
                        }
                        T_STATUS => {
                            let s = format!(
                                "{{\"holder\":{},\"child\":{},\"cols\":{},\"rows\":{},\"viewers\":{},\"active\":{},\"seen\":{:.3},\"ring\":{},\"started\":{:.3},\"cwd\":{}}}",
                                std::process::id(), child, cols, rows, viewers, active, last_seen, ring.len(), started, json_str(&cwd.to_string_lossy())
                            );
                            c.out.extend(frame(T_STATUS_R, s.as_bytes()));
                        }
                        _ => {}
                    }
                }
            }
            if !c.out.is_empty() {
                let (a, _) = c.out.as_slices();
                match c.stream.write(a) {
                    Ok(n) => { c.out.drain(..n); }
                    Err(e) if e.kind() == io::ErrorKind::WouldBlock => {}
                    Err(_) => dead[i] = true,
                }
                if c.out.len() > MAX_CLIENT_BACKLOG {
                    dead[i] = true;
                }
            }
        }
        let mut i = 0;
        clients.retain(|_| { let keep = !dead[i]; i += 1; keep });
        // The owner left: the most recent remaining active viewer takes over the size.
        if owner.map_or(false, |o| !clients.iter().any(|c| c.id == o)) {
            owner = None;
            if let Some(c) = clients.iter().rev().find(|c| c.attached && c.size.is_some()) {
                owner = Some(c.id);
                let (nc, nr) = c.size.unwrap();
                if (nc, nr) != (cols, rows) {
                    cols = nc;
                    rows = nr;
                    screen.resize(cols, rows);
                    set_winsize(master, cols, rows);
                }
            }
        }
    }
}

// ---------- attach ----------

fn connect(sock: &Path) -> UnixStream {
    UnixStream::connect(sock).unwrap_or_else(|e| {
        let exit = sock.with_extension("exit");
        if let Ok(code) = std::fs::read_to_string(&exit) {
            die(format!("{} has ended (exit {})", sock.display(), code.trim()));
        }
        die(format!("nothing is holding {} ({e})", sock.display()))
    })
}

struct RawMode(Option<libc::termios>);
impl RawMode {
    fn enter() -> RawMode {
        unsafe {
            if libc::isatty(0) == 0 {
                return RawMode(None);
            }
            let mut t: libc::termios = std::mem::zeroed();
            if libc::tcgetattr(0, &mut t) != 0 {
                return RawMode(None);
            }
            let saved = t;
            libc::cfmakeraw(&mut t);
            libc::tcsetattr(0, libc::TCSANOW, &t);
            RawMode(Some(saved))
        }
    }
}
impl Drop for RawMode {
    fn drop(&mut self) {
        if let Some(t) = self.0 {
            unsafe { libc::tcsetattr(0, libc::TCSANOW, &t) };
        }
    }
}

fn attach(rest: &[String]) {
    let o = parse(rest);
    let sock = o.sock.clone().unwrap_or_else(|| usage());
    let mut stream = connect(&sock);
    let sig_r = signal_pipe(&[libc::SIGWINCH]);
    unsafe { libc::signal(libc::SIGPIPE, libc::SIG_IGN) };
    let (cols, rows) = get_winsize(1).or_else(|| get_winsize(0)).unwrap_or((o.cols, o.rows));
    let code = {
        let _raw = RawMode::enter();
        // Passive: 0x0 in HELLO and no RESIZE, so this viewer never changes the agent's size.
        let hello = if o.passive { size_payload(0, 0) } else { size_payload(cols, rows) };
        if stream.write_all(&frame(T_HELLO, &hello)).is_err() {
            die("holder closed the connection");
        }
        attach_loop(&mut stream, sig_r, o.detach_key, o.passive)
    };
    std::process::exit(code);
}

fn attach_loop(stream: &mut UnixStream, sig_r: RawFd, detach_key: bool, passive: bool) -> i32 {
    let sfd = stream.as_raw_fd();
    let mut inbuf = Vec::new();
    let mut tmp = vec![0u8; 64 * 1024];
    let mut stdin_open = true;
    loop {
        let mut pfds = vec![
            libc::pollfd { fd: sfd, events: libc::POLLIN, revents: 0 },
            libc::pollfd { fd: sig_r, events: libc::POLLIN, revents: 0 },
        ];
        if stdin_open {
            pfds.push(libc::pollfd { fd: 0, events: libc::POLLIN, revents: 0 });
        }
        if unsafe { libc::poll(pfds.as_mut_ptr(), pfds.len() as _, -1) } < 0 {
            if io::Error::last_os_error().kind() == io::ErrorKind::Interrupted { continue }
            return 1;
        }
        if pfds[1].revents & libc::POLLIN != 0 {
            drain_fd(sig_r);
            if passive {
                continue;
            }
            if let Some((c, r)) = get_winsize(1).or_else(|| get_winsize(0)) {
                if stream.write_all(&frame(T_RESIZE, &size_payload(c, r))).is_err() { return 1 }
            }
        }
        if stdin_open && pfds.len() > 2 && pfds[2].revents & (libc::POLLIN | libc::POLLHUP) != 0 {
            let n = unsafe { libc::read(0, tmp.as_mut_ptr() as *mut _, tmp.len()) };
            if n <= 0 {
                stdin_open = false; // keep showing output (e.g. piped attach)
            } else {
                let data = &tmp[..n as usize];
                if detach_key && data.contains(&0x1c) {
                    return 0;
                }
                if stream.write_all(&frame(T_INPUT, data)).is_err() { return 1 }
            }
        }
        if pfds[0].revents & (libc::POLLIN | libc::POLLHUP | libc::POLLERR) != 0 {
            match stream.read(&mut tmp) {
                Ok(0) | Err(_) => return 1,
                Ok(n) => inbuf.extend_from_slice(&tmp[..n]),
            }
            while let Some((t, p)) = take_frame(&mut inbuf) {
                match t {
                    T_OUTPUT => raw_write_all(1, &p),
                    T_EXIT => {
                        let code = if p.len() >= 4 { i32::from_be_bytes([p[0], p[1], p[2], p[3]]) } else { 0 };
                        return code;
                    }
                    _ => {}
                }
            }
        }
    }
}

// ---------- status ----------

fn saved_screen(rest: &[String]) {
    let o = parse(rest);
    let path = o.file.unwrap_or_else(|| usage());
    let data = std::fs::read(path).unwrap_or_else(|e| die(e));
    let rendered = if data.starts_with(SCREEN_MAGIC) {
        if data.len() < 9 || read_size(&data[5..9]).is_none() { die("invalid saved screen header"); }
        data[9..].to_vec()
    } else if data.starts_with(b"PHSC") {
        die("unsupported saved screen version; update peer-hold")
    } else {
        // Legacy files were unframed rings and recorded no dimensions. They may
        // start inside an escape sequence or UTF-8 codepoint. As with the old
        // replay, discard the cut-off first line when the ring filled (1 MiB).
        let mut bytes = data.as_slice();
        if data.len() >= 1 << 20 {
            if let Some(pos) = data.iter().position(|&b| b == b'\n') { bytes = &data[pos + 1..]; }
        }
        let mut screen = screen::Screen::new(o.cols, o.rows);
        screen.feed(bytes);
        screen.frozen()
    };
    io::stdout().write_all(&rendered).unwrap_or_else(|e| die(e));
}

fn status(rest: &[String]) {
    let o = parse(rest);
    let sock = o.sock.clone().unwrap_or_else(|| usage());
    let mut stream = connect(&sock);
    stream.set_read_timeout(Some(Duration::from_secs(2))).ok();
    stream.write_all(&frame(T_STATUS, &[])).unwrap_or_else(|_| die("holder closed the connection"));
    let mut buf = Vec::new();
    let mut tmp = [0u8; 4096];
    loop {
        match stream.read(&mut tmp) {
            Ok(0) | Err(_) => die("no status reply"),
            Ok(n) => buf.extend_from_slice(&tmp[..n]),
        }
        while let Some((t, p)) = take_frame(&mut buf) {
            if t == T_STATUS_R {
                println!("{}", String::from_utf8_lossy(&p));
                return;
            }
        }
    }
}

// ---------- tail ----------

/// Escape sequences out, lines in. Carriage return without newline means "redraw this line",
/// so only what follows the last CR on a line survives.
fn plain_lines(data: &[u8]) -> Vec<String> {
    let mut lines = Vec::new();
    let mut line: Vec<u8> = Vec::new();
    let mut i = 0;
    while i < data.len() {
        let b = data[i];
        match b {
            0x1b => {
                i += 1;
                match data.get(i) {
                    Some(b'[') => {
                        i += 1;
                        let params = i;
                        while i < data.len() && !(0x40..=0x7e).contains(&data[i]) { i += 1 }
                        match data.get(i) {
                            // Ink (Claude Code) moves down a row instead of printing a newline.
                            Some(b'B') | Some(b'E') => { lines.push(String::from_utf8_lossy(&line).trim_end().to_string()); line.clear() }
                            // Column jumps stand between words ("Claude Code ESC[24G v2.1"); one space keeps them apart.
                            Some(b'G') if line.last().map_or(false, |&c| c != b' ') => line.push(b' '),
                            Some(b'C') => {
                                let n: usize = std::str::from_utf8(&data[params..i]).ok().and_then(|t| t.parse().ok()).unwrap_or(1);
                                line.extend(std::iter::repeat(b' ').take(n.min(200)));
                            }
                            _ => {}
                        }
                    }
                    Some(b']') | Some(b'P') | Some(b'_') | Some(b'^') => {
                        // OSC/DCS/APC/PM: until BEL or ESC \\
                        i += 1;
                        while i < data.len() && data[i] != 0x07 && !(data[i] == 0x1b && data.get(i + 1) == Some(&b'\\')) { i += 1 }
                        if i < data.len() && data[i] == 0x1b { i += 1 }
                    }
                    Some(b'(') | Some(b')') | Some(b'#') => i += 1,
                    _ => {}
                }
            }
            b'\n' => { lines.push(String::from_utf8_lossy(&line).trim_end().to_string()); line.clear() }
            b'\r' => {
                // The PTY turns "\r\n" into "\r\r\n"; only a CR not heading into a newline redraws.
                let mut j = i + 1;
                while data.get(j) == Some(&b'\r') { j += 1 }
                // Look through the cursor/colour sequences right after the CR: a row step among them
                // (Ink writes "\r ESC[2C ESC[1B") means the next row, not a redraw of this one.
                let mut down = false;
                let mut k = j;
                while data.get(k) == Some(&0x1b) && data.get(k + 1) == Some(&b'[') {
                    k += 2;
                    while data.get(k).map_or(false, |c| !(0x40..=0x7e).contains(c)) { k += 1 }
                    if matches!(data.get(k), Some(b'B') | Some(b'E')) { down = true; break }
                    k += 1;
                }
                if data.get(j) != Some(&b'\n') && !down { line.clear() }
            }
            b'\t' => line.push(b' '),
            0..=0x1f | 0x7f => {}
            _ => line.push(b),
        }
        i += 1;
    }
    if !line.is_empty() { lines.push(String::from_utf8_lossy(&line).trim_end().to_string()) }
    lines
}

/// What a full-screen program (Codex, Claude's fullscreen) shows right now, as rows of text. Its output has no lines
/// to read back: it paints cells by position, so the plain log ran every frame into one line and an agent reading it
/// saw the first frame ("Resuming session…") long after the real answer (11/07 sm feedback). The snapshot a passive
/// viewer gets is the screen itself. None for a program on the normal screen, where the log still has more history.
fn full_screen_text(sock: &Path) -> Option<Vec<String>> {
    let mut stream = UnixStream::connect(sock).ok()?;
    stream.set_read_timeout(Some(Duration::from_secs(3))).ok();
    stream.write_all(&frame(T_HELLO, &size_payload(0, 0))).ok()?;
    let mut buf = Vec::new();
    let mut tmp = vec![0u8; 64 * 1024];
    loop {
        let n = stream.read(&mut tmp).ok()?;
        if n == 0 { return None }
        buf.extend_from_slice(&tmp[..n]);
        while let Some((t, p)) = take_frame(&mut buf) {
            if t == T_OUTPUT { return screen::text_if_alternate(&p) }
        }
    }
}

fn tail(rest: &[String]) {
    let o = parse(rest);
    let sock = o.sock.clone().unwrap_or_else(|| usage());
    let mut stream = connect(&sock);
    stream.set_read_timeout(Some(Duration::from_secs(3))).ok();
    stream.write_all(&frame(T_TAIL, &[])).unwrap_or_else(|_| die("holder closed the connection"));
    let mut buf = Vec::new();
    let mut tmp = vec![0u8; 64 * 1024];
    loop {
        match stream.read(&mut tmp) {
            Ok(0) | Err(_) => die("no tail reply"),
            Ok(n) => buf.extend_from_slice(&tmp[..n]),
        }
        while let Some((t, p)) = take_frame(&mut buf) {
            if t == T_TAIL_R {
                if o.raw {
                    raw_write_all(1, &p);
                } else {
                    let lines: Vec<String> = full_screen_text(&sock)
                        .unwrap_or_else(|| plain_lines(&p))
                        .into_iter().filter(|l| !l.is_empty()).collect();
                    let start = lines.len().saturating_sub(o.lines);
                    for l in &lines[start..] { println!("{l}") }
                }
                return;
            }
        }
    }
}
