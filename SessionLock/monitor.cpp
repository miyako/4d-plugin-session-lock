#include "monitor.h"
#include <system_error>

namespace sessionlock {
Monitor::Monitor(std::unique_ptr<Backend> backend) : backend_(std::move(backend)) {}
Monitor::~Monitor() { stop(); }

Result Monitor::start() {
    std::lock_guard<std::mutex> guard(lifecycle_);
    if (cache_.state().running && cache_.state().available)
        return {true, cache_.state()};
    Error cleanup = backend_->stop();
    if (cleanup) {
        cache_.stopped(cleanup);
        return {false, cache_.state()};
    }
    cache_.begin();
    Error error;
    try {
        error = backend_->start(cache_);
    } catch (const std::system_error&) {
        error = {10001, "Native monitor thread could not start"};
    }
    if (error) {
        const auto stopError = backend_->stop();
        cache_.stopped(stopError ? stopError : error);
    } else {
        cache_.ready();
    }
    const auto state = cache_.state();
    return {!error && state.available, state};
}

Result Monitor::stop() {
    std::lock_guard<std::mutex> guard(lifecycle_);
    Error error = backend_->stop();
    cache_.stopped(error);
    return {!error, cache_.state()};
}

State Monitor::state() {
    std::lock_guard<std::mutex> guard(lifecycle_);
    return cache_.state();
}

Batch Monitor::events() {
    std::lock_guard<std::mutex> guard(lifecycle_);
    return cache_.drain();
}
}
