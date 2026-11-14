//! Reconnects restore screen state, never historical terminal commands.
//! The raw ring is only a diagnostic log. In particular, replaying a terminal
//! query can cause a new viewer to inject its answer into an unrelated prompt.
use std::collections::BTreeMap;
use std::num::NonZeroU16;

/// Rows of text of a snapshot whose program is on the alternate screen. The snapshot does not say its size, but it
/// places everything absolutely, so a parser larger than any terminal reads it without wrapping.
pub fn text_if_alternate(snapshot: &[u8]) -> Option<Vec<String>> {
    let mut parser = vt100::Parser::new(dimension(500), dimension(1000), 0);
    parser.process(snapshot);
    let screen = parser.screen();
    screen.alternate_screen().then(|| {
        screen.contents().lines().map(|line| line.trim_end().to_string()).collect()
    })
}

pub struct Screen {
    terminal: vt100::Parser,
    parser: vte::Parser,
    modes: Modes,
    pending: Pending,
}

fn dimension(n: u16) -> NonZeroU16 {
    NonZeroU16::new(n.max(1)).unwrap()
}

impl Screen {
    pub fn new(cols: u16, rows: u16) -> Self {
        Self {
            terminal: vt100::Parser::new(dimension(rows), dimension(cols), 0),
            parser: vte::Parser::new(),
            modes: Modes::default(),
            pending: Pending::default(),
        }
    }

    pub fn feed(&mut self, data: &[u8]) {
        self.terminal.process(data);
        self.parser.advance(&mut self.modes, data);
        self.pending.feed(data);
    }

    pub fn resize(&mut self, cols: u16, rows: u16) {
        self.terminal.set_size(dimension(rows), dimension(cols));
        // Like the viewer, a real size change resets the scroll region.
        self.modes.margins = [None; 2];
    }

    pub fn snapshot(&self) -> Vec<u8> {
        let mut out = self.frozen();
        // A PTY read can split an escape or UTF-8 character. New viewers need
        // that unfinished prefix before subsequent live bytes, but no history.
        out.extend_from_slice(&self.pending.bytes);
        out
    }

    pub fn frozen(&self) -> Vec<u8> {
        let screen = self.terminal.screen();
        let (rows, cols) = screen.size();
        let mut scratch = vt100::Parser::new(rows, cols, 0);
        *scratch.screen_mut() = screen.clone();
        // Restore the normal buffer too: leaving an alternate-screen program
        // after reconnect must reveal the shell that was underneath it.
        scratch.process(b"\x1b[?47l");
        let mut out = b"\x18\x1b[?2026h\x1b[?1049l\x1b[?47l\x1b[?6l\x1b[r\x1b[?7h\x1b[4l".to_vec();
        if screen.alternate_screen() {
            out.extend(scratch.screen().state_formatted().bytes());
            restore_position(
                &mut out,
                scratch.screen(),
                self.modes.margins[0],
                self.modes.origin[0],
            );
            // 1049 saves the primary cursor before entering the alternate buffer.
            out.extend_from_slice(b"\x1b[?1049h");
        }
        // ESC 7/8 saved cursor and attributes survive a reconnect too.
        *scratch.screen_mut() = screen.clone();
        scratch.process(b"\x1b8");
        let active = usize::from(screen.alternate_screen());
        restore_position(
            &mut out,
            scratch.screen(),
            self.modes.margins[active],
            self.modes.saved_origin[active],
        );
        out.extend_from_slice(b"\x1b7");
        if self.modes.margins[active].is_some() || self.modes.saved_origin[active] {
            out.extend_from_slice(b"\x1b[?6l\x1b[r");
        }
        out.extend(screen.state_formatted().bytes());
        // Only persistent, non-destructive viewer modes are restored. Historical
        // queries, bells, clipboard writes, window resizes and sync begin/end are
        // deliberately absent from a snapshot.
        for (&mode, &on) in &self.modes.dec {
            if matches!(
                mode,
                1 | 7 | 9 | 25 | 1000 | 1002 | 1003 | 1004 | 1005 | 1006 | 1015 | 2004
            ) {
                out.extend(format!("\x1b[?{}{}", mode, if on { 'h' } else { 'l' }).bytes());
            }
        }
        if let Some((base, rest)) = self.modes.keyboard.split_first() {
            out.extend(format!("\x1b[<32u\x1b[={}u", base).bytes());
            for flags in rest {
                out.extend(format!("\x1b[>{}u", flags).bytes());
            }
        }
        restore_position(
            &mut out,
            screen,
            self.modes.margins[active],
            self.modes.origin[active],
        );
        out.extend_from_slice(b"\x1b[?2026l");
        out
    }
}

// Apply buffer-local margins and origin before positioning the cursor. vt100's
// serializer uses absolute CUP rows; translate those when origin is relative.
fn restore_position(
    out: &mut Vec<u8>,
    screen: &vt100::Screen,
    margins: Option<(u16, u16)>,
    origin: bool,
) {
    let (top, bottom) = margins.unwrap_or((1, screen.size().0.get()));
    if margins.is_some() || origin {
        out.extend(
            format!(
                "\x1b[{};{}r\x1b[?6{}",
                top,
                bottom,
                if origin { 'h' } else { 'l' }
            )
            .bytes(),
        );
    }
    let cursor = screen.cursor_state_formatted();
    let mut parts = cursor.split("\x1b[");
    out.extend(parts.next().unwrap().bytes());
    for part in parts {
        out.extend_from_slice(b"\x1b[");
        // Cursor state contains only cursor visibility/position and SGR. CUP
        // also appears when redrawing a last-column glyph to restore pending wrap.
        if origin {
            if let Some(end) = part.find(|c: char| c.is_ascii_alphabetic()) {
                if part.as_bytes()[end] == b'H' {
                    if let Some((row, col)) = part[..end].split_once(';') {
                        if let Ok(row) = row.parse::<u16>() {
                            out.extend(
                                format!("{};{}{}", row.saturating_sub(top - 1), col, &part[end..])
                                    .bytes(),
                            );
                            continue;
                        }
                    }
                }
            }
        }
        out.extend(part.bytes());
    }
    out.extend(screen.attributes_formatted().bytes());
}

#[derive(Default)]
struct Modes {
    dec: BTreeMap<u16, bool>,
    margins: [Option<(u16, u16)>; 2],
    origin: [bool; 2],
    saved_origin: [bool; 2],
    alternate: bool,
    keyboard: Vec<u16>,
}

impl vte::Perform for Modes {
    fn csi_dispatch(
        &mut self,
        params: &vte::Params,
        intermediates: &[u8],
        ignore: bool,
        action: char,
    ) {
        if ignore {
            return;
        }
        if intermediates == b"?" && matches!(action, 'h' | 'l') {
            for param in params {
                if let [mode] = param {
                    let on = action == 'h';
                    let active = usize::from(self.alternate);
                    match *mode {
                        6 => self.origin[active] = on,
                        47 => self.alternate = on,
                        1049 => {
                            if on {
                                self.saved_origin[0] = self.origin[0];
                                self.margins[1] = None;
                                self.origin[1] = false;
                                self.saved_origin[1] = false;
                            } else {
                                self.origin[0] = self.saved_origin[0];
                            }
                            self.alternate = on;
                        }
                        _ => {}
                    }
                    if matches!(
                        mode,
                        1 | 7 | 9 | 25 | 1000 | 1002 | 1003 | 1004 | 1005 | 1006 | 1015 | 2004
                    ) {
                        self.dec.insert(*mode, action == 'h');
                    }
                }
            }
        } else if intermediates.is_empty() && action == 'r' {
            let p: Vec<_> = params.iter().map(|p| p[0]).collect();
            self.margins[usize::from(self.alternate)] = match p.as_slice() {
                [top, bottom] if *bottom > (*top).max(1) => Some(((*top).max(1), *bottom)),
                _ => None,
            };
        } else if intermediates == b"!" && action == 'p' {
            *self = Self::default();
        } else if action == 'u' {
            let p: Vec<_> = params.iter().map(|p| p[0]).collect();
            let flags = p.first().copied().unwrap_or(0);
            match intermediates {
                b">" => {
                    // Kitty keyboard enhancement stack (used by Codex/crossterm).
                    if self.keyboard.is_empty() {
                        self.keyboard.push(0);
                    }
                    if self.keyboard.len() == 32 {
                        self.keyboard.remove(0);
                    }
                    self.keyboard.push(flags);
                }
                b"<" => {
                    let len = self
                        .keyboard
                        .len()
                        .saturating_sub(usize::from(flags.max(1)))
                        .max(1);
                    self.keyboard.truncate(len);
                }
                b"=" => {
                    if self.keyboard.is_empty() {
                        self.keyboard.push(0);
                    }
                    let current = self.keyboard.last_mut().unwrap();
                    *current = match p.get(1).copied().unwrap_or(1) {
                        2 => *current | flags,
                        3 => *current & !flags,
                        _ => flags,
                    };
                }
                _ => {}
            }
        }
    }
    fn esc_dispatch(&mut self, intermediates: &[u8], ignore: bool, byte: u8) {
        if !ignore && intermediates.is_empty() {
            let active = usize::from(self.alternate);
            match byte {
                b'c' => *self = Self::default(),
                b'7' => self.saved_origin[active] = self.origin[active],
                b'8' => self.origin[active] = self.saved_origin[active],
                _ => {}
            }
        }
    }
}

/// Lexical boundary only; VT interpretation belongs to vt100/vte. Keep at most
/// a small prefix, so even an unfinished OSC cannot make attachment cost history-sized.
#[derive(Default)]
struct Pending {
    state: u8, // ground, ESC, CSI, string, string ESC, UTF-8, ESC intermediate
    utf8: u8,
    bytes: Vec<u8>,
}

impl Pending {
    fn feed(&mut self, data: &[u8]) {
        for &b in data {
            if matches!(b, 0x18 | 0x1a) {
                self.state = 0;
                self.bytes.clear();
                continue;
            }
            if self.state == 0 {
                self.bytes.clear();
                match b {
                    0x1b => self.state = 1,
                    0xc2..=0xdf => {
                        self.state = 5;
                        self.utf8 = 1;
                    }
                    0xe0..=0xef => {
                        self.state = 5;
                        self.utf8 = 2;
                    }
                    0xf0..=0xf4 => {
                        self.state = 5;
                        self.utf8 = 3;
                    }
                    _ => continue,
                }
            } else {
                match self.state {
                    1 => {
                        self.state = match b {
                            b'[' => 2,
                            b']' | b'P' | b'X' | b'^' | b'_' => 3,
                            0x20..=0x2f => 6,
                            0x1b => {
                                self.bytes.clear();
                                1
                            }
                            0x30..=0x7e => 0,
                            _ => 1,
                        }
                    }
                    2 | 6 => {
                        if b == 0x1b {
                            self.bytes.clear();
                            self.state = 1;
                        } else if (0x40..=0x7e).contains(&b)
                            || (self.state == 6 && (0x30..=0x7e).contains(&b))
                        {
                            self.state = 0;
                        }
                    }
                    3 => {
                        if b == 0x1b {
                            self.state = 4;
                        } else if b == 7 && self.bytes.starts_with(b"\x1b]") {
                            self.state = 0;
                        }
                    }
                    4 => {
                        if b == b'\\' {
                            self.state = 0;
                        } else {
                            self.state = 3;
                        }
                    }
                    5 => {
                        if b & 0xc0 == 0x80 {
                            self.utf8 -= 1;
                            if self.utf8 == 0 {
                                self.state = 0;
                            }
                        } else {
                            self.state = 0;
                            self.bytes.clear();
                            self.feed(&[b]);
                            continue;
                        }
                    }
                    _ => unreachable!(),
                }
            }
            if self.state == 0 {
                self.bytes.clear();
            } else {
                self.bytes.push(b);
                if self.bytes.len() > 4096 {
                    // Ignore an oversized unfinished control string on the new
                    // viewer until its terminator. Live viewers still receive it.
                    self.bytes = match self.state {
                        2 => b"\x1b[0:".to_vec(),
                        6 => b"\x1b ".to_vec(),
                        _ if self.bytes.starts_with(b"\x1b]") => b"\x1b]999999;".to_vec(),
                        _ => b"\x1b^".to_vec(),
                    };
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_full_screen_program_reads_back_as_its_rows() {
        let mut screen = Screen::new(40, 10);
        screen.feed(b"shell line\r\n\x1b[?1049h\x1b[2J\x1b[1;3Hfirst frame\x1b[1;3H\x1b[Kanswer\x1b[4;5Hsecond row");
        let text = text_if_alternate(&screen.snapshot()).expect("on the alternate screen");
        let rows: Vec<&str> = text.iter().map(String::as_str).filter(|l| !l.is_empty()).collect();
        assert_eq!(rows, ["  answer", "    second row"]);

        let mut plain = Screen::new(40, 10);
        plain.feed(b"just a shell\r\n");
        assert_eq!(text_if_alternate(&plain.snapshot()), None);
    }

    fn viewer(cols: u16, rows: u16, bytes: &[u8]) -> vt100::Parser {
        let mut p = vt100::Parser::new(dimension(rows), dimension(cols), 0);
        p.process(bytes);
        p
    }

    fn assert_screen(a: &vt100::Screen, b: &vt100::Screen) {
        assert_eq!(a.contents(), b.contents());
        assert_eq!(a.cursor_position(), b.cursor_position());
        assert_eq!(a.alternate_screen(), b.alternate_screen());
        let (rows, cols) = a.size();
        for row in 0..rows.get() {
            for col in 0..cols.get() {
                assert_eq!(a.cell(row, col), b.cell(row, col), "cell {row},{col}");
            }
        }
    }

    #[test]
    fn screen_is_bounded_and_has_no_historical_side_effects() {
        let mut s = Screen::new(80, 24);
        for _ in 0..10000 {
            s.feed(b"old output\r\n\x1b[c\x1b[6n\x1b]11;?\x07");
        }
        s.feed(b"\x1b[H\x1b[2Jcurrent\r\n\x1b[31mred\x1b[0m\x1b]52;c;payload\x07\x07");
        let snap = s.snapshot();
        assert!(snap.len() < 1024, "{}", snap.len());
        for needle in [
            b"old output".as_slice(),
            b"\x1b[c",
            b"\x1b[6n",
            b"\x1b]",
            b"\x07",
        ] {
            assert!(!snap.windows(needle.len()).any(|w| w == needle));
        }
        assert_screen(s.terminal.screen(), viewer(80, 24, &snap).screen());
        assert_eq!(snap.windows(7).filter(|w| *w == b"current").count(), 1);
    }

    #[test]
    fn utf8_and_escape_fragments_continue_after_every_attach_boundary() {
        let bytes =
            "\x1b[2;3H\x1b[38;2;40;80;120m한글🙂e\u{301}\x1b[0m\x1b]0;title\x07\r\nfinished"
                .as_bytes();
        for split in 0..=bytes.len() {
            let mut s = Screen::new(40, 12);
            s.feed(&bytes[..split]);
            let mut v = viewer(40, 12, &s.snapshot());
            s.feed(&bytes[split..]);
            v.process(&bytes[split..]);
            assert_screen(s.terminal.screen(), v.screen());
        }
    }

    #[test]
    fn alternate_screen_exit_and_saved_cursor_continue() {
        let mut s = Screen::new(40, 12);
        s.feed(b"shell prompt\x1b[?1049h\x1b[2;3H\x1b[32malternate\x1b7\x1b[5;6Hnow\x1b[?2004h");
        let mut v = viewer(40, 12, &s.snapshot());
        assert_screen(s.terminal.screen(), v.screen());
        assert!(v.screen().bracketed_paste());
        for continuation in [b"\x1b8!".as_slice(), b"\x1b[?1049lreturned"] {
            s.feed(continuation);
            v.process(continuation);
            assert_screen(s.terminal.screen(), v.screen());
        }
    }

    #[test]
    fn scroll_region_origin_and_pending_wrap_continue() {
        for setup in [
            b"\x1b[3;8r\x1b[?6h\x1b[6;1Hlast".as_slice(),
            b"1234567890123456789012345678901234567890",
            b"\x1b[3;8r\x1b[?6h\x1b[2;1H1234567890123456789012345678901234567890",
        ] {
            let mut s = Screen::new(40, 12);
            s.feed(setup);
            let mut v = viewer(40, 12, &s.snapshot());
            assert_screen(s.terminal.screen(), v.screen());
            s.feed(b"next\r\nline");
            v.process(b"next\r\nline");
            assert_screen(s.terminal.screen(), v.screen());
        }
    }

    #[test]
    fn origin_and_margins_do_not_leak_between_screen_buffers() {
        let mut s = Screen::new(40, 12);
        s.feed(b"\x1b[3;8r\x1b[?6h\x1b[2;1Hprimary\x1b[?1049halternate");
        let mut v = viewer(40, 12, &s.snapshot());
        assert_screen(s.terminal.screen(), v.screen());
        s.feed(b"\x1b[?1049l\x1b[1;1Hback");
        v.process(b"\x1b[?1049l\x1b[1;1Hback");
        assert_screen(s.terminal.screen(), v.screen());
    }

    #[test]
    fn terminal_reset_discards_old_viewer_modes() {
        let mut s = Screen::new(80, 24);
        s.feed(b"\x1b[?1004h\x1b[?2004h\x1bcreset");
        let snap = s.snapshot();
        assert!(!snap.windows(8).any(|s| s == b"\x1b[?1004h"));
        assert!(!viewer(80, 24, &snap).screen().bracketed_paste());
    }

    #[test]
    fn keyboard_stack_and_frozen_partial_sequences() {
        let mut s = Screen::new(80, 24);
        s.feed(b"\x1b[>1u\x1b[>3u\x1b[<u\x1b[=2;2u\x1b[6");
        let snap = s.snapshot();
        assert!(snap.windows(5).any(|w| w == b"\x1b[>3u"));
        assert!(snap.ends_with(b"\x1b[6"));
        assert!(s.frozen().ends_with(b"\x1b[?2026l"));
        let mut copy = Screen::new(80, 24);
        copy.feed(&snap);
        assert_eq!(s.modes.keyboard, copy.modes.keyboard);
    }

    #[test]
    fn oversized_partial_osc_still_terminates() {
        let mut s = Screen::new(80, 24);
        s.feed(b"\x1b]0;");
        s.feed(&vec![b'x'; 12000]);
        let mut v = viewer(80, 24, &s.snapshot());
        s.feed(b"\x07visible");
        v.process(b"\x07visible");
        assert_screen(s.terminal.screen(), v.screen());
        assert_eq!(v.screen().contents(), "visible");
    }
}
