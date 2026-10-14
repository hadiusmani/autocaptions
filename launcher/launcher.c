/*
 * AutoCaption.exe — Windows launcher.
 *
 * 1. If the AutoCaption server isn't running, starts it silently (no console window)
 *    from .venv\Scripts\pythonw.exe app.py. On first run (no .venv yet) it runs the
 *    visible setup script instead.
 * 2. Opens the UI as a standalone app window (Edge or Chrome in --app mode).
 * 3. The server shuts itself down a few minutes after the window is closed
 *    (heartbeat-based, see auto_exit_watchdog in app.py).
 *
 * Build (cross-compile from Linux/macOS):
 *   python -m ziglang cc -target x86_64-windows-gnu -O2 -mwindows \
 *       launcher.c launcher.rc -o AutoCaption.exe -lws2_32 -lshlwapi
 */
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <windows.h>
#include <shlwapi.h>
#include <shellapi.h>
#include <wchar.h>

#define PORT 5055
static const wchar_t *URL = L"http://127.0.0.1:5055";

static int server_up(void) {
    SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s == INVALID_SOCKET) return 0;
    struct sockaddr_in a = {0};
    a.sin_family = AF_INET;
    a.sin_port = htons(PORT);
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    int ok = connect(s, (struct sockaddr *)&a, sizeof a) == 0;
    closesocket(s);
    return ok;
}

static int exists(const wchar_t *p) { return GetFileAttributesW(p) != INVALID_FILE_ATTRIBUTES; }

static void msg(const wchar_t *text) { MessageBoxW(NULL, text, L"AutoCaption", MB_OK | MB_ICONINFORMATION); }

/* Find Edge or Chrome for a clean standalone app window. */
static int find_browser(wchar_t *out, DWORD n) {
    const wchar_t *cands[] = {
        L"%ProgramFiles(x86)%\\Microsoft\\Edge\\Application\\msedge.exe",
        L"%ProgramFiles%\\Microsoft\\Edge\\Application\\msedge.exe",
        L"%ProgramFiles%\\Google\\Chrome\\Application\\chrome.exe",
        L"%ProgramFiles(x86)%\\Google\\Chrome\\Application\\chrome.exe",
        L"%LocalAppData%\\Google\\Chrome\\Application\\chrome.exe",
    };
    for (int i = 0; i < (int)(sizeof cands / sizeof *cands); i++) {
        ExpandEnvironmentStringsW(cands[i], out, n);
        if (exists(out)) return 1;
    }
    return 0;
}

int WINAPI wWinMain(HINSTANCE h, HINSTANCE p, PWSTR cmd, int show) {
    (void)h; (void)p; (void)cmd; (void)show;
    WSADATA wsa;
    WSAStartup(MAKEWORD(2, 2), &wsa);

    /* App folder = folder containing this exe. */
    wchar_t dir[MAX_PATH];
    GetModuleFileNameW(NULL, dir, MAX_PATH);
    PathRemoveFileSpecW(dir);
    SetCurrentDirectoryW(dir);

    HANDLE server = NULL;
    if (!server_up()) {
        wchar_t py[MAX_PATH];
        swprintf(py, MAX_PATH, L"%ls\\.venv\\Scripts\\pythonw.exe", dir);
        if (!exists(py)) {
            /* First run: do the visible one-time setup, which also starts the app. */
            wchar_t bat[MAX_PATH];
            swprintf(bat, MAX_PATH, L"%ls\\Start AutoCaption.bat", dir);
            if (!exists(bat)) { msg(L"Can't find 'Start AutoCaption.bat' next to AutoCaption.exe."); return 1; }
            ShellExecuteW(NULL, L"open", bat, NULL, dir, SW_SHOWNORMAL);
            return 0;
        }
        wchar_t cl[2 * MAX_PATH];
        swprintf(cl, 2 * MAX_PATH, L"\"%ls\" app.py --no-browser --auto-exit", py);
        STARTUPINFOW si = {sizeof si};
        PROCESS_INFORMATION pi;
        if (!CreateProcessW(NULL, cl, NULL, NULL, FALSE, CREATE_NO_WINDOW, NULL, dir, &si, &pi)) {
            msg(L"Couldn't start AutoCaption. Try running 'Start AutoCaption.bat' once.");
            return 1;
        }
        CloseHandle(pi.hThread);
        server = pi.hProcess;
        /* Wait for it to come up (first launch after boot can take a few seconds). */
        for (int i = 0; i < 300 && !server_up(); i++) {
            if (WaitForSingleObject(server, 100) == WAIT_OBJECT_0) {
                msg(L"AutoCaption stopped while starting. See autocaption.log in the app folder.");
                return 1;
            }
        }
    }

    wchar_t browser[MAX_PATH];
    if (!find_browser(browser, MAX_PATH)) {
        /* No Edge/Chrome: open in the default browser and leave the server running. */
        ShellExecuteW(NULL, L"open", URL, NULL, NULL, SW_SHOWNORMAL);
        if (server) CloseHandle(server);
        return 0;
    }

    /* Dedicated profile so this window is its own process we can wait on. */
    wchar_t prof[MAX_PATH];
    ExpandEnvironmentStringsW(L"%LocalAppData%\\AutoCaption\\window", prof, MAX_PATH);
    wchar_t cl[4 * MAX_PATH];
    swprintf(cl, 4 * MAX_PATH,
             L"\"%ls\" --app=%ls --user-data-dir=\"%ls\" --window-size=1440,960 "
             L"--no-first-run --no-default-browser-check --disable-features=Translate",
             browser, URL, prof);
    STARTUPINFOW si = {sizeof si};
    PROCESS_INFORMATION pi;
    if (!CreateProcessW(NULL, cl, NULL, NULL, FALSE, 0, NULL, NULL, &si, &pi)) {
        ShellExecuteW(NULL, L"open", URL, NULL, NULL, SW_SHOWNORMAL);
        if (server) CloseHandle(server);
        return 0;
    }
    CloseHandle(pi.hThread);
    if (server) CloseHandle(server);
    CloseHandle(pi.hProcess);
    return 0;
}
