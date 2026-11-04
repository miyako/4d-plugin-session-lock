#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0A00
#endif
#include <windows.h>
#include <wtsapi32.h>

#include "backend.h"

#include <atomic>
#include <condition_variable>
#include <cwchar>
#include <mutex>
#include <system_error>
#include <thread>

#ifdef _MSC_VER
#pragma comment(lib, "Wtsapi32.lib")
#pragma comment(lib, "User32.lib")
#endif

namespace sessionlock {
namespace {
Error nativeError(const char* message, DWORD code = GetLastError()) noexcept {
    return {code ? code : ERROR_GEN_FAILURE, message};
}

class WindowsBackend final : public Backend {
public:
    ~WindowsBackend() override { stop(); }

    Error start(Cache& cache) override {
        if (thread_.joinable()) return {};
        cache_ = &cache;
        stopRequested_.store(false);
        startupDone_ = false;
        startupError_ = {};
        result_ = {};
        callbackError_ = {};
        reconcile_ = false;
        suspended_ = false;
        stopEvent_ = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        if (!stopEvent_) return nativeError("CreateEventW failed");
        try {
            thread_ = std::thread([this] { run(); });
        } catch (const std::system_error& error) {
            Error failure{static_cast<std::uint32_t>(error.code().value()),
                          "Cannot start Windows notification thread"};
            if (!failure.code) failure.code = ERROR_GEN_FAILURE;
            CloseHandle(stopEvent_);
            stopEvent_ = nullptr;
            return failure;
        } catch (...) {
            CloseHandle(stopEvent_);
            stopEvent_ = nullptr;
            return {ERROR_GEN_FAILURE, "Cannot start Windows notification thread"};
        }
        Error failure;
        {
            std::unique_lock<std::mutex> lock(startupMutex_);
            startupCondition_.wait(lock, [this] { return startupDone_; });
            failure = startupError_;
        }
        if (failure) {
            stop();
            return failure;
        }
        return {};
    }

    Error stop() override {
        if (!thread_.joinable()) return {};
        stopRequested_.store(true);
        Error wakeError;
        if (!SetEvent(stopEvent_)) wakeError = nativeError("SetEvent failed during stop");
        // The bounded wait also permits joining if SetEvent itself fails.
        thread_.join();
        if (!CloseHandle(stopEvent_) && !result_)
            result_ = nativeError("CloseHandle failed for stop event");
        stopEvent_ = nullptr;
        cache_ = nullptr;
        return result_ ? result_ : wakeError;
    }

private:
    struct WtsMemory {
        LPWSTR data = nullptr;
        ~WtsMemory() { if (data) WTSFreeMemory(data); }
    };

    void remember(Error error) noexcept {
        if (!result_) result_ = error;
    }

    void handshake(Error error = {}) {
        {
            std::lock_guard<std::mutex> lock(startupMutex_);
            if (startupDone_) return;
            startupError_ = error;
            startupDone_ = true;
        }
        startupCondition_.notify_one();
    }

    void invalidate(const char* source) {
        cache_->snapshot(cache_->state().revision, Lock::unknown, source);
        reconcile_ = true;
    }

    static LRESULT CALLBACK windowProcedure(HWND window, UINT message,
                                             WPARAM wparam, LPARAM lparam) noexcept {
        auto* self = reinterpret_cast<WindowsBackend*>(
            GetWindowLongPtrW(window, GWLP_USERDATA));
        if (message == WM_NCCREATE) {
            self = static_cast<WindowsBackend*>(
                reinterpret_cast<CREATESTRUCTW*>(lparam)->lpCreateParams);
            SetLastError(ERROR_SUCCESS);
            if (!SetWindowLongPtrW(window, GWLP_USERDATA,
                                  reinterpret_cast<LONG_PTR>(self)) && GetLastError()) {
                self->callbackError_ = nativeError("SetWindowLongPtrW failed");
                return FALSE;
            }
        }
        if (self) {
            try {
                return self->onMessage(window, message, wparam, lparam);
            } catch (...) {
                // No C++ exception may unwind through user32's callback boundary.
                self->callbackError_ = {ERROR_GEN_FAILURE, "Windows callback exception"};
                return 0;
            }
        }
        return DefWindowProcW(window, message, wparam, lparam);
    }

    LRESULT onMessage(HWND window, UINT message, WPARAM wparam, LPARAM lparam) {
        if (message == WM_WTSSESSION_CHANGE) {
            if (static_cast<DWORD>(lparam) != session_) return 0;
            switch (wparam) {
                case WTS_SESSION_LOCK:
                    cache_->signal(Lock::locked, "wts-notification");
                    break;
                case WTS_SESSION_UNLOCK:
                    cache_->signal(Lock::unlocked, "wts-notification");
                    break;
                case WTS_CONSOLE_CONNECT:
                case WTS_CONSOLE_DISCONNECT:
                case WTS_REMOTE_CONNECT:
                case WTS_REMOTE_DISCONNECT:
                case WTS_SESSION_LOGON:
                case WTS_SESSION_LOGOFF:
                    // Connection state is not lock state, and is never an event.
                    invalidate("wts-session-change");
                    break;
                default:
                    break;
            }
            return 0;
        }
        if (message == WM_POWERBROADCAST) {
            if (wparam == PBT_APMSUSPEND) {
                suspended_ = true;
                invalidate("windows-suspend");
            } else if (wparam == PBT_APMRESUMEAUTOMATIC ||
                       wparam == PBT_APMRESUMESUSPEND ||
                       wparam == PBT_APMRESUMECRITICAL) {
                suspended_ = false;
                invalidate("windows-resume");
            }
            return TRUE;
        }
        if (message == WM_QUERYENDSESSION) return TRUE;
        if (message == WM_ENDSESSION || message == WM_CLOSE) return 0;
        if (message == WM_DESTROY && !destroying_)
            callbackError_ = {ERROR_INVALID_WINDOW_HANDLE, "Notification window destroyed unexpectedly"};
        if (message == WM_NCDESTROY)
            SetWindowLongPtrW(window, GWLP_USERDATA, 0);
        return DefWindowProcW(window, message, wparam, lparam);
    }

    Error pump() {
        MSG message{};
        while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) {
            if (message.message == WM_QUIT)
                return {ERROR_OPERATION_ABORTED, "Windows notification message loop quit"};
            TranslateMessage(&message);
            DispatchMessageW(&message);
            if (callbackError_) return callbackError_;
            if (stopRequested_.load()) break;
        }
        return callbackError_;
    }

    Error snapshot() {
        reconcile_ = false;
        const auto revision = cache_->state().revision;
        Lock lock = Lock::unknown;
        Error error;
        WtsMemory memory;
        DWORD bytes = 0;
        if (!WTSQuerySessionInformationW(WTS_CURRENT_SERVER_HANDLE, session_,
                                         WTSSessionInfoEx, &memory.data, &bytes)) {
            error = nativeError("WTSQuerySessionInformationW failed");
        } else if (!memory.data || bytes < sizeof(WTSINFOEXW)) {
            error = {ERROR_INVALID_DATA, "WTSSessionInfoEx returned missing or truncated data"};
        } else {
            const auto* info = reinterpret_cast<const WTSINFOEXW*>(memory.data);
            if (info->Level != 1 || info->Data.WTSInfoExLevel1.SessionId != session_) {
                error = {ERROR_INVALID_DATA, "WTSSessionInfoEx returned an invalid level or session"};
            } else {
                // Windows 10+ semantics; the reversed Windows 7 flags are not supported.
                const LONG flags = info->Data.WTSInfoExLevel1.SessionFlags;
                if (flags == WTS_SESSIONSTATE_LOCK)
                    lock = Lock::locked;
                else if (flags == WTS_SESSIONSTATE_UNLOCK)
                    lock = Lock::unlocked;
                else
                    error = {ERROR_INVALID_DATA, "WTSSessionInfoEx lock state is unknown"};
            }
        }
        // WTS messages can have queued while querying. Apply them first, so an
        // older snapshot cannot overwrite their revision or fabricate an event.
        if (Error failure = pump()) return failure;
        if (!stopRequested_.load() && !suspended_)
            cache_->snapshot(revision, lock, "wts-snapshot", error);
        return {};
    }

    Error initialize() {
        if (!ProcessIdToSessionId(GetCurrentProcessId(), &session_))
            return nativeError("ProcessIdToSessionId failed");
        if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                               GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                               reinterpret_cast<LPCWSTR>(&windowProcedure), &module_))
            return nativeError("GetModuleHandleExW failed");
        if (std::swprintf(className_, sizeof(className_) / sizeof(className_[0]),
                          L"SessionLock.%lu.%p", static_cast<unsigned long>(GetCurrentProcessId()),
                          static_cast<void*>(this)) < 0)
            return {ERROR_INVALID_DATA, "Cannot form notification window class name"};
        WNDCLASSEXW windowClass{};
        windowClass.cbSize = sizeof(windowClass);
        windowClass.lpfnWndProc = &windowProcedure;
        windowClass.hInstance = module_;
        windowClass.lpszClassName = className_;
        if (!RegisterClassExW(&windowClass))
            return nativeError("RegisterClassExW failed");
        classRegistered_ = true;
        // A hidden top-level window receives power broadcasts; HWND_MESSAGE does not.
        window_ = CreateWindowExW(0, className_, L"", WS_OVERLAPPED,
                                  0, 0, 0, 0, nullptr, nullptr, module_, this);
        if (!window_) return callbackError_ ? callbackError_ : nativeError("CreateWindowExW failed");
        if (!WTSRegisterSessionNotification(window_, NOTIFY_FOR_THIS_SESSION))
            return nativeError("WTSRegisterSessionNotification failed");
        registered_ = true;
        return snapshot();
    }

    void cleanup() noexcept {
        if (registered_) {
            if (!WTSUnRegisterSessionNotification(window_))
                remember(nativeError("WTSUnRegisterSessionNotification failed"));
            registered_ = false;
        }
        if (window_) {
            destroying_ = true;
            if (!DestroyWindow(window_))
                remember(nativeError("DestroyWindow failed"));
            window_ = nullptr;
            destroying_ = false;
        }
        if (classRegistered_) {
            if (!UnregisterClassW(className_, module_))
                remember(nativeError("UnregisterClassW failed"));
            classRegistered_ = false;
        }
    }

    void run() noexcept {
        try {
            remember(initialize());
            if (!result_) {
                handshake();
                while (!stopRequested_.load()) {
                    if (Error error = pump()) { remember(error); break; }
                    if (reconcile_ && !suspended_) {
                        if (Error error = snapshot()) { remember(error); break; }
                        continue;
                    }
                    const DWORD wait = MsgWaitForMultipleObjects(
                        1, &stopEvent_, FALSE, 250, QS_ALLINPUT);
                    if (wait == WAIT_OBJECT_0) break;
                    if (wait == WAIT_OBJECT_0 + 1 || wait == WAIT_TIMEOUT) continue;
                    remember(wait == WAIT_FAILED
                                 ? nativeError("MsgWaitForMultipleObjects failed")
                                 : Error{ERROR_GEN_FAILURE, "Unexpected Windows message wait result"});
                    break;
                }
            }
        } catch (...) {
            remember({ERROR_GEN_FAILURE, "Windows notification thread exception"});
        }
        cleanup();
        if (result_) {
            try { cache_->fail(result_, "windows-backend", true); }
            catch (...) { /* Preserve the native failure and still release the startup waiter. */ }
        }
        handshake(result_);
    }

    Cache* cache_ = nullptr;
    std::thread thread_;
    HANDLE stopEvent_ = nullptr;
    std::atomic<bool> stopRequested_{false};
    std::mutex startupMutex_;
    std::condition_variable startupCondition_;
    bool startupDone_ = false;
    Error startupError_;
    Error result_;
    Error callbackError_;
    DWORD session_ = 0;
    HMODULE module_ = nullptr;
    HWND window_ = nullptr;
    wchar_t className_[96]{};
    bool classRegistered_ = false;
    bool registered_ = false;
    bool destroying_ = false;
    bool reconcile_ = false;
    bool suspended_ = false;
};
}

std::unique_ptr<Backend> makeBackend() {
    return std::make_unique<WindowsBackend>();
}
}
