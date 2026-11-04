#include "backend.h"
#import <Foundation/Foundation.h>
#import <CoreGraphics/CoreGraphics.h>
#include <atomic>
#include <condition_variable>
#include <thread>

namespace {
using namespace sessionlock;
constexpr const char* snapshotSource = "macos.CGSession.undocumented";

void snapshot(Cache& cache) {
    const auto revision = cache.state().revision;
    CFDictionaryRef dictionary = CGSessionCopyCurrentDictionary();
    Lock lock = Lock::unknown;
    Error error{20001, "No GUI session dictionary is available"};
    if (dictionary) {
        CFTypeRef value = CFDictionaryGetValue(dictionary, CFSTR("CGSSessionScreenIsLocked"));
        error = {20002, "Undocumented lock snapshot key is absent or not Boolean"};
        if (value && CFGetTypeID(value) == CFBooleanGetTypeID()) {
            lock = CFBooleanGetValue(static_cast<CFBooleanRef>(value)) ? Lock::locked : Lock::unlocked;
            error = {};
        }
        CFRelease(dictionary);
    }
    // Let already queued notifications win over the queried baseline.
    CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.0, true);
    cache.snapshot(revision, lock, snapshotSource, error);
}
}

@interface SLObserver : NSObject
@property(nonatomic, assign) sessionlock::Cache* cache;
- (void)locked:(NSNotification*)notification;
- (void)unlocked:(NSNotification*)notification;
@end
@implementation SLObserver
- (void)locked:(NSNotification*)notification {
    (void)notification;
    self.cache->signal(sessionlock::Lock::locked, "macos.distributed.undocumented");
}
- (void)unlocked:(NSNotification*)notification {
    (void)notification;
    self.cache->signal(sessionlock::Lock::unlocked, "macos.distributed.undocumented");
}
@end

namespace sessionlock {
class MacBackend final : public Backend {
public:
    ~MacBackend() override { stop(); }
    Error start(Cache& cache) override {
        if (thread_.joinable()) return {};
        done_ = false;
        started_ = false;
        error_ = {};
        thread_ = std::thread([this, &cache] { run(cache); });
        std::unique_lock<std::mutex> guard(mutex_);
        condition_.wait(guard, [this] { return started_; });
        return error_;
    }
    Error stop() override {
        done_ = true;
        {
            std::lock_guard<std::mutex> guard(mutex_);
            if (loop_) {
                CFRunLoopStop(loop_);
                CFRunLoopWakeUp(loop_);
            }
        }
        if (thread_.joinable()) thread_.join();
        return error_;
    }
private:
    void run(Cache& cache) {
        @autoreleasepool {
            SLObserver* observer = [SLObserver new];
            observer.cache = &cache;
            NSDistributedNotificationCenter* center = [NSDistributedNotificationCenter defaultCenter];
            NSPort* port = nil;
            @try {
                if (!observer || !center)
                    @throw [NSException exceptionWithName:@"SessionLockStartup" reason:@"Observer unavailable" userInfo:nil];
                [center addObserver:observer selector:@selector(locked:)
                    name:@"com.apple.screenIsLocked" object:nil
                    suspensionBehavior:NSNotificationSuspensionBehaviorDeliverImmediately];
                [center addObserver:observer selector:@selector(unlocked:)
                    name:@"com.apple.screenIsUnlocked" object:nil
                    suspensionBehavior:NSNotificationSuspensionBehaviorDeliverImmediately];
                // Add a source so an otherwise empty run loop remains serviceable.
                port = [NSMachPort port];
                if (!port)
                    @throw [NSException exceptionWithName:@"SessionLockStartup" reason:@"Run loop port unavailable" userInfo:nil];
                [[NSRunLoop currentRunLoop] addPort:port forMode:NSDefaultRunLoopMode];
                snapshot(cache);
                {
                    std::lock_guard<std::mutex> guard(mutex_);
                    loop_ = CFRunLoopGetCurrent();
                    CFRetain(loop_);
                    started_ = true;
                }
                condition_.notify_one();
                while (!done_) {
                    @autoreleasepool {
                        CFRunLoopRunInMode(kCFRunLoopDefaultMode, 1.0, true);
                    }
                }
            } @catch (NSException* exception) {
                (void)exception;
                const Error error{20003, "Distributed notification observer failed"};
                {
                    std::lock_guard<std::mutex> guard(mutex_);
                    error_ = error;
                }
                cache.fail(error, "macos.distributed.undocumented", true);
            } @finally {
                [center removeObserver:observer];
                if (port) {
                    [[NSRunLoop currentRunLoop] removePort:port forMode:NSDefaultRunLoopMode];
                    [port invalidate];
                }
                observer.cache = nullptr;
                {
                    std::lock_guard<std::mutex> guard(mutex_);
                    if (loop_) CFRelease(loop_);
                    loop_ = nullptr;
                    started_ = true;
                }
                condition_.notify_one();
            }
        }
    }
    std::thread thread_;
    std::atomic<bool> done_{false};
    std::mutex mutex_;
    std::condition_variable condition_;
    CFRunLoopRef loop_ = nullptr;
    bool started_ = false;
    Error error_;
};
std::unique_ptr<Backend> makeBackend() { return std::make_unique<MacBackend>(); }
}
