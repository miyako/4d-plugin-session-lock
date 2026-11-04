#include "core.h"
#include <chrono>

namespace sessionlock {
const char* name(Lock value) {
    switch (value) {
        case Lock::locked: return "locked";
        case Lock::unlocked: return "unlocked";
        default: return "unknown";
    }
}

std::int64_t nowMilliseconds() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}

void Cache::begin() {
    std::lock_guard<std::mutex> guard(mutex_);
    const auto generation = state_.generation + 1;
    const auto sequence = state_.sequence;
    const auto revision = state_.revision + 1;
    state_ = {};
    state_.generation = generation;
    state_.sequence = sequence;
    state_.revision = revision;
    state_.running = true;
    head_ = count_ = 0;
    dropped_ = 0;
    fatal_ = false;
}

void Cache::ready() {
    std::lock_guard<std::mutex> guard(mutex_);
    if (state_.running && !fatal_) state_.available = true;
}

void Cache::stopped(Error error) {
    std::lock_guard<std::mutex> guard(mutex_);
    state_.running = state_.available = false;
    state_.lock = Lock::unknown;
    state_.observedAt = 0;
    state_.source = "none";
    state_.error = error;
    ++state_.revision;
}

void Cache::fail(Error error, const char* source, bool fatal) {
    std::lock_guard<std::mutex> guard(mutex_);
    if (!state_.running) return;
    state_.lock = Lock::unknown;
    state_.source = source;
    state_.observedAt = nowMilliseconds();
    state_.error = error;
    if (fatal) {
        state_.available = false;
        fatal_ = true;
    }
    ++state_.revision;
}

State Cache::state() const {
    std::lock_guard<std::mutex> guard(mutex_);
    return state_;
}

bool Cache::snapshot(std::uint64_t expectedRevision, Lock lock, const char* source, Error error) {
    std::lock_guard<std::mutex> guard(mutex_);
    if (!state_.running || state_.revision != expectedRevision) return false;
    state_.lock = lock;
    state_.source = source;
    state_.observedAt = nowMilliseconds();
    state_.error = error;
    ++state_.revision;
    return true;
}

void Cache::signal(Lock lock, const char* source) {
    std::lock_guard<std::mutex> guard(mutex_);
    if (!state_.running || lock == Lock::unknown) return;
    const auto previous = state_.lock;
    state_.lock = lock;
    state_.source = source;
    state_.observedAt = nowMilliseconds();
    state_.error = {};
    ++state_.revision;
    if (previous == lock) return;
    Event event{previous, lock, source, state_.observedAt, ++state_.sequence, state_.generation};
    if (count_ == queueCapacity) {
        head_ = (head_ + 1) % queueCapacity;
        --count_;
        ++dropped_;
        ++state_.droppedTotal;
    }
    queue_[(head_ + count_) % queueCapacity] = event;
    ++count_;
}

Batch Cache::drain() {
    std::lock_guard<std::mutex> guard(mutex_);
    Batch batch;
    batch.count = count_;
    batch.dropped = dropped_;
    batch.state = state_;
    for (std::size_t i = 0; i < count_; ++i)
        batch.events[i] = queue_[(head_ + i) % queueCapacity];
    head_ = count_ = 0;
    dropped_ = 0;
    return batch;
}
}
