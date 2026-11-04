#pragma once

#include <array>
#include <cstdint>
#include <mutex>

namespace sessionlock {
enum class Lock { unknown, unlocked, locked };
const char* name(Lock value);
std::int64_t nowMilliseconds();

struct Error {
    std::uint32_t code = 0;
    const char* message = "";
    explicit operator bool() const { return code != 0; }
};

struct State {
    Lock lock = Lock::unknown;
    bool running = false;
    bool available = false;
    const char* source = "none";
    std::int64_t observedAt = 0;
    std::uint64_t revision = 0;
    std::uint64_t generation = 0;
    std::uint64_t sequence = 0;
    std::uint64_t droppedTotal = 0;
    Error error;
};

struct Event {
    Lock previous = Lock::unknown;
    Lock lock = Lock::unknown;
    const char* source = "none";
    std::int64_t observedAt = 0;
    std::uint64_t sequence = 0;
    std::uint64_t generation = 0;
};

constexpr std::size_t queueCapacity = 128;
struct Batch {
    std::array<Event, queueCapacity> events{};
    std::size_t count = 0;
    std::uint64_t dropped = 0;
    State state;
};

class Cache {
public:
    void begin();
    void ready();
    void stopped(Error error = {});
    void fail(Error error, const char* source, bool fatal = false);
    State state() const;
    // A snapshot cannot overwrite a notification received during the query.
    bool snapshot(std::uint64_t expectedRevision, Lock lock, const char* source, Error error = {});
    void signal(Lock lock, const char* source);
    Batch drain();
private:
    mutable std::mutex mutex_;
    State state_;
    std::array<Event, queueCapacity> queue_{};
    std::size_t head_ = 0;
    std::size_t count_ = 0;
    std::uint64_t dropped_ = 0;
    bool fatal_ = false;
};
}
