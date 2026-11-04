#include "serialization.h"

#include <atomic>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <limits>
#include <memory>
#include <system_error>
#include <thread>
#include <utility>
#include <vector>

namespace {
using namespace sessionlock;

[[noreturn]] void requirementFailed(const char* expression, int line) {
    std::cerr << "core_tests.cpp:" << line << ": REQUIRE(" << expression << ") failed\n";
    std::abort();
}
#define REQUIRE(expression) ((expression) ? static_cast<void>(0) : requirementFailed(#expression, __LINE__))

bool equal(const char* a, const char* b) { return std::strcmp(a, b) == 0; }

void requireStopped(const State& state) {
    REQUIRE(!state.running);
    REQUIRE(!state.available);
    REQUIRE(state.lock == Lock::unknown);
    REQUIRE(state.observedAt == 0);
    REQUIRE(equal(state.source, "none"));
}

void initialAndSnapshots() {
    Cache cache;
    const auto initial = cache.state();
    requireStopped(initial);
    REQUIRE(initial.revision == 0);
    REQUIRE(initial.generation == 0);
    REQUIRE(initial.sequence == 0);
    REQUIRE(initial.droppedTotal == 0);
    REQUIRE(!initial.error);
    REQUIRE(cache.drain().count == 0);
    REQUIRE(!cache.snapshot(initial.revision, Lock::locked, "snapshot"));
    cache.ready();
    cache.signal(Lock::locked, "ignored");
    cache.fail({1, "ignored"}, "ignored", true);
    requireStopped(cache.state());
    REQUIRE(cache.state().revision == initial.revision);

    cache.begin();
    auto state = cache.state();
    REQUIRE(state.running);
    REQUIRE(!state.available);
    REQUIRE(state.lock == Lock::unknown);
    REQUIRE(state.generation == 1);
    REQUIRE(state.revision > initial.revision);
    cache.ready();
    REQUIRE(cache.state().available);
    REQUIRE(cache.snapshot(state.revision, Lock::unlocked, "snapshot"));
    state = cache.state();
    REQUIRE(state.lock == Lock::unlocked);
    REQUIRE(equal(state.source, "snapshot"));
    REQUIRE(state.observedAt != 0);
    REQUIRE(state.sequence == 0);
    REQUIRE(cache.drain().count == 0);
    REQUIRE(!cache.snapshot(state.revision - 1, Lock::locked, "stale"));
    REQUIRE(cache.state().revision == state.revision);

    cache.signal(Lock::locked, "notification");
    REQUIRE(!cache.snapshot(state.revision, Lock::unlocked, "racing snapshot"));
    REQUIRE(cache.state().lock == Lock::locked);
    REQUIRE(equal(cache.state().source, "notification"));
    REQUIRE(cache.drain().count == 1);
    REQUIRE(cache.snapshot(cache.state().revision, Lock::unknown, "missing"));
    REQUIRE(cache.state().lock == Lock::unknown);
    REQUIRE(!cache.state().error);
    REQUIRE(cache.drain().count == 0);
    REQUIRE(cache.snapshot(cache.state().revision, Lock::unknown, "query", {7, "query failed"}));
    REQUIRE(cache.state().error.code == 7);
    REQUIRE(equal(cache.state().error.message, "query failed"));
    REQUIRE(cache.drain().count == 0);
}

void signalsAndFailures() {
    Cache cache;
    cache.begin();
    cache.ready();
    const auto revision = cache.state().revision;
    cache.signal(Lock::unknown, "ignored");
    REQUIRE(cache.state().revision == revision);
    cache.signal(Lock::locked, "first");
    auto batch = cache.drain();
    REQUIRE(batch.count == 1);
    REQUIRE(batch.events[0].previous == Lock::unknown);
    REQUIRE(batch.events[0].lock == Lock::locked);
    REQUIRE(batch.events[0].sequence == 1);
    REQUIRE(batch.events[0].generation == 1);
    REQUIRE(batch.events[0].observedAt == batch.state.observedAt);
    REQUIRE(equal(batch.events[0].source, "first"));
    const auto beforeDuplicate = cache.state();
    cache.signal(Lock::locked, "duplicate");
    REQUIRE(cache.drain().count == 0);
    REQUIRE(cache.state().sequence == 1);
    REQUIRE(cache.state().revision > beforeDuplicate.revision);
    REQUIRE(equal(cache.state().source, "duplicate"));
    REQUIRE(!cache.snapshot(beforeDuplicate.revision, Lock::unlocked, "stale duplicate"));

    cache.fail({11, "temporarily missing"}, "query");
    auto state = cache.state();
    REQUIRE(state.running && state.available);
    REQUIRE(state.lock == Lock::unknown);
    REQUIRE(state.observedAt != 0);
    REQUIRE(state.error.code == 11);
    REQUIRE(equal(state.source, "query"));
    REQUIRE(cache.drain().count == 0);
    cache.signal(Lock::locked, "recovered");
    batch = cache.drain();
    REQUIRE(batch.count == 1);
    REQUIRE(batch.events[0].previous == Lock::unknown);
    REQUIRE(batch.events[0].sequence == 2);
    REQUIRE(!batch.state.error);

    cache.fail({12, "native source lost"}, "backend", true);
    state = cache.state();
    REQUIRE(state.running && !state.available);
    REQUIRE(state.lock == Lock::unknown);
    REQUIRE(state.error.code == 12);
    REQUIRE(cache.drain().count == 0);
    REQUIRE(!cache.snapshot(state.revision - 1, Lock::unlocked, "stale failure"));
}

void fatalBeforeReady() {
    Cache cache;
    cache.begin();
    const auto generation = cache.state().generation;
    cache.fail({13, "worker failed before ready"}, "early fatal", true);
    const auto failed = cache.state();
    REQUIRE(failed.running && !failed.available);
    REQUIRE(failed.lock == Lock::unknown);
    REQUIRE(failed.error.code == 13);
    cache.ready();
    cache.ready();
    const auto afterReady = cache.state();
    REQUIRE(afterReady.running && !afterReady.available);
    REQUIRE(afterReady.lock == Lock::unknown);
    REQUIRE(afterReady.error.code == 13);
    REQUIRE(equal(afterReady.error.message, "worker failed before ready"));
    REQUIRE(equal(afterReady.source, "early fatal"));
    REQUIRE(afterReady.revision == failed.revision);
    REQUIRE(afterReady.observedAt == failed.observedAt);
    REQUIRE(cache.drain().count == 0);

    cache.begin();
    cache.ready();
    const auto recovered = cache.state();
    REQUIRE(recovered.running && recovered.available);
    REQUIRE(recovered.generation == generation + 1);
    REQUIRE(recovered.lock == Lock::unknown);
    REQUIRE(!recovered.error);
    REQUIRE(recovered.observedAt == 0);
    REQUIRE(equal(recovered.source, "none"));
}

void overflowAndGenerations() {
    REQUIRE(queueCapacity == 128);
    Cache cache;
    cache.begin();
    cache.ready();
    constexpr std::size_t emitted = queueCapacity + 37;
    for (std::size_t i = 0; i < emitted; ++i)
        cache.signal(i % 2 == 0 ? Lock::locked : Lock::unlocked, "ring");
    auto batch = cache.drain();
    REQUIRE(batch.count == queueCapacity);
    REQUIRE(batch.dropped == 37);
    REQUIRE(batch.state.droppedTotal == 37);
    REQUIRE(batch.state.sequence == emitted);
    for (std::size_t i = 0; i < batch.count; ++i) {
        const auto sequence = 38 + i;
        const auto& event = batch.events[i];
        REQUIRE(event.sequence == sequence);
        REQUIRE(event.generation == 1);
        REQUIRE(event.lock == (sequence % 2 ? Lock::locked : Lock::unlocked));
        REQUIRE(event.previous == (sequence % 2 ? Lock::unlocked : Lock::locked));
        REQUIRE(equal(event.source, "ring"));
    }
    const auto retained = cache.state();
    const auto empty = cache.drain();
    REQUIRE(empty.count == 0 && empty.dropped == 0);
    REQUIRE(empty.state.sequence == retained.sequence);
    REQUIRE(empty.state.droppedTotal == 37);
    REQUIRE(empty.state.lock == retained.lock);
    REQUIRE(empty.state.observedAt == retained.observedAt);
    REQUIRE(equal(empty.state.source, retained.source));

    // Leave an overflowing queue pending across stop, including its drop report.
    for (std::size_t i = 0; i < queueCapacity + 5; ++i)
        cache.signal(i % 2 == 0 ? Lock::unlocked : Lock::locked, "pending");
    const auto beforeStop = cache.state();
    cache.stopped({19, "cleanup"});
    requireStopped(cache.state());
    REQUIRE(cache.state().error.code == 19);
    REQUIRE(cache.state().sequence == beforeStop.sequence);
    REQUIRE(cache.state().generation == beforeStop.generation);
    cache.signal(Lock::locked, "late");
    cache.ready();
    REQUIRE(!cache.snapshot(beforeStop.revision, Lock::locked, "late"));
    REQUIRE(!cache.snapshot(cache.state().revision, Lock::locked, "late current"));
    batch = cache.drain();
    REQUIRE(batch.count == queueCapacity);
    REQUIRE(batch.dropped == 5);
    REQUIRE(batch.state.droppedTotal == 42);
    REQUIRE(batch.events.back().sequence == beforeStop.sequence);
    REQUIRE(batch.events.back().lock == beforeStop.lock);
    requireStopped(batch.state);
    REQUIRE(cache.drain().dropped == 0);

    cache.begin();
    cache.signal(Lock::locked, "new generation");
    for (std::size_t i = 0; i < queueCapacity + 2; ++i)
        cache.signal(i % 2 == 0 ? Lock::unlocked : Lock::locked, "discarded");
    REQUIRE(cache.state().droppedTotal > 0);
    const auto beforeBegin = cache.state();
    cache.begin();
    const auto fresh = cache.state();
    REQUIRE(fresh.generation == beforeBegin.generation + 1);
    REQUIRE(fresh.revision > beforeBegin.revision);
    REQUIRE(fresh.sequence == beforeBegin.sequence);
    REQUIRE(fresh.droppedTotal == 0);
    REQUIRE(fresh.lock == Lock::unknown);
    REQUIRE(fresh.running && !fresh.available);
    REQUIRE(fresh.observedAt == 0 && equal(fresh.source, "none"));
    REQUIRE(!fresh.error);
    batch = cache.drain();
    REQUIRE(batch.count == 0 && batch.dropped == 0);
    REQUIRE(!cache.snapshot(beforeBegin.revision, Lock::locked, "old generation"));
    cache.signal(Lock::unlocked, "fresh");
    batch = cache.drain();
    REQUIRE(batch.count == 1);
    REQUIRE(batch.events[0].sequence == fresh.sequence + 1);
    REQUIRE(batch.events[0].generation == fresh.generation);
    REQUIRE(batch.events[0].previous == Lock::unknown);
}

void concurrentCache() {
    Cache cache;
    cache.begin();
    cache.ready();
    std::atomic<bool> go{false};
    std::atomic<unsigned> finished{0};
    std::atomic<std::uint64_t> consumed{0}, dropped{0};
    std::vector<std::thread> threads;
    constexpr unsigned producers = 6;
    for (unsigned p = 0; p < producers; ++p) {
        threads.emplace_back([&, p] {
            while (!go.load()) std::this_thread::yield();
            for (unsigned i = 0; i < 2000; ++i)
                cache.signal((i + p) % 2 ? Lock::locked : Lock::unlocked, "parallel");
            ++finished;
        });
    }
    for (unsigned r = 0; r < 3; ++r) {
        threads.emplace_back([&] {
            while (!go.load()) std::this_thread::yield();
            std::uint64_t last = 0;
            do {
                const auto state = cache.state();
                REQUIRE(state.running && state.available);
                REQUIRE(state.generation == 1);
                REQUIRE(state.sequence >= last);
                REQUIRE(state.droppedTotal <= state.sequence);
                REQUIRE(!state.error);
                REQUIRE(state.lock == Lock::unknown || equal(state.source, "parallel"));
                last = state.sequence;
            } while (finished.load() != producers);
        });
    }
    const auto consume = [&](const Batch& batch, std::uint64_t& last) {
        REQUIRE(batch.count <= queueCapacity);
        REQUIRE(batch.state.generation == 1);
        REQUIRE(batch.state.droppedTotal <= batch.state.sequence);
        for (std::size_t i = 0; i < batch.count; ++i) {
            const auto& event = batch.events[i];
            REQUIRE(event.sequence > last);
            if (i) {
                REQUIRE(event.sequence == batch.events[i - 1].sequence + 1);
                REQUIRE(event.previous == batch.events[i - 1].lock);
            }
            REQUIRE(event.sequence <= batch.state.sequence);
            REQUIRE(event.generation == 1);
            REQUIRE(event.lock == Lock::locked || event.lock == Lock::unlocked);
            REQUIRE(event.previous != event.lock);
            REQUIRE(equal(event.source, "parallel"));
            REQUIRE(event.observedAt != 0);
            last = event.sequence;
        }
        if (batch.count) {
            REQUIRE(batch.events[batch.count - 1].sequence == batch.state.sequence);
            REQUIRE(batch.events[batch.count - 1].lock == batch.state.lock);
        }
        consumed.fetch_add(batch.count);
        dropped.fetch_add(batch.dropped);
    };
    for (unsigned d = 0; d < 3; ++d) {
        threads.emplace_back([&] {
            while (!go.load()) std::this_thread::yield();
            std::uint64_t last = 0;
            do {
                consume(cache.drain(), last);
            } while (finished.load() != producers);
        });
    }
    go = true;
    for (auto& thread : threads) thread.join();
    std::uint64_t last = 0;
    consume(cache.drain(), last);
    const auto final = cache.state();
    REQUIRE(final.sequence > 0);
    REQUIRE(final.sequence <= producers * 2000);
    REQUIRE(consumed.load() + dropped.load() == final.sequence);
    REQUIRE(dropped.load() == final.droppedTotal);
    REQUIRE(cache.drain().count == 0);
    REQUIRE(cache.drain().dropped == 0);
}

struct Calls {
    std::atomic<unsigned> starts{0}, stops{0}, active{0};
};

class FakeBackend final : public Backend {
public:
    explicit FakeBackend(std::shared_ptr<Calls> calls = std::make_shared<Calls>())
        : calls(std::move(calls)) {}
    std::shared_ptr<Calls> calls;
    Cache* cache = nullptr;
    std::vector<Error> startErrors;
    std::vector<Error> stopErrors;
    bool throwOnStart = false;
    bool signalOnStart = false;
    bool fatalOnStart = false;

    Error start(Cache& target) override {
        Entry entry(*calls);
        cache = &target;
        const auto index = calls->starts.fetch_add(1);
        if (throwOnStart)
            throw std::system_error(std::make_error_code(std::errc::resource_unavailable_try_again));
        REQUIRE(target.state().running && !target.state().available);
        REQUIRE(target.snapshot(target.state().revision, Lock::unlocked, "fake snapshot"));
        if (signalOnStart) target.signal(Lock::locked, "fake signal");
        if (fatalOnStart)
            target.fail({46, "worker failed during start"}, "fake early fatal", true);
        return index < startErrors.size() ? startErrors[index] : Error{};
    }
    Error stop() override {
        Entry entry(*calls);
        const auto index = calls->stops.fetch_add(1);
        return index < stopErrors.size() ? stopErrors[index] : Error{};
    }
private:
    struct Entry {
        Calls& calls;
        explicit Entry(Calls& calls) : calls(calls) {
            REQUIRE(calls.active.fetch_add(1) == 0);
            std::this_thread::yield();
        }
        ~Entry() { REQUIRE(calls.active.fetch_sub(1) == 1); }
    };
};

void monitorLifecycle() {
    auto calls = std::make_shared<Calls>();
    {
        auto backend = std::make_unique<FakeBackend>(calls);
        auto* fake = backend.get();
        Monitor monitor(std::move(backend));
        requireStopped(monitor.state());
        REQUIRE(monitor.events().count == 0);
        auto result = monitor.start();
        REQUIRE(result.success && result.state.running && result.state.available);
        REQUIRE(result.state.generation == 1);
        REQUIRE(result.state.lock == Lock::unlocked);
        REQUIRE(calls->starts == 1 && calls->stops == 1);
        REQUIRE(monitor.events().count == 0);
        const auto revision = result.state.revision;
        result = monitor.start();
        REQUIRE(result.success && result.state.revision == revision);
        REQUIRE(calls->starts == 1 && calls->stops == 1);
        fake->cache->signal(Lock::locked, "queued");
        REQUIRE(monitor.stop().success);
        requireStopped(monitor.state());
        REQUIRE(monitor.events().count == 1);
        REQUIRE(monitor.stop().success);
        requireStopped(monitor.state());
        REQUIRE(calls->starts == 1);
        result = monitor.start();
        REQUIRE(result.success && result.state.generation == 2);
        REQUIRE(result.state.sequence == 1);
        REQUIRE(monitor.events().count == 0);
        fake->cache->fail({31, "fatal"}, "fake fatal", true);
        REQUIRE(!monitor.state().available);
        result = monitor.start();
        REQUIRE(result.success && result.state.available);
        REQUIRE(result.state.generation == 3);
        REQUIRE(!result.state.error);
        REQUIRE(calls->starts == 3);
    }
    REQUIRE(calls->active == 0);
    REQUIRE(calls->stops == 6); // Includes cleanup on retry and destructor while running.
}

void monitorFailures() {
    {
        auto backend = std::make_unique<FakeBackend>();
        auto* fake = backend.get();
        fake->fatalOnStart = true;
        Monitor monitor(std::move(backend));
        const auto result = monitor.start();
        REQUIRE(!result.success);
        REQUIRE(result.state.running && !result.state.available);
        REQUIRE(result.state.lock == Lock::unknown);
        REQUIRE(result.state.error.code == 46);
        REQUIRE(equal(result.state.error.message, "worker failed during start"));
        REQUIRE(equal(result.state.source, "fake early fatal"));
        REQUIRE(result.state.generation == 1);
        REQUIRE(fake->calls->starts == 1 && fake->calls->stops == 1);
        REQUIRE(!monitor.state().available);
        REQUIRE(monitor.events().count == 0);
        fake->fatalOnStart = false;
        const auto recovered = monitor.start();
        REQUIRE(recovered.success);
        REQUIRE(recovered.state.running && recovered.state.available);
        REQUIRE(recovered.state.generation == 2);
        REQUIRE(!recovered.state.error);
        REQUIRE(fake->calls->starts == 2 && fake->calls->stops == 2);
    }
    {
        auto backend = std::make_unique<FakeBackend>();
        auto* fake = backend.get();
        fake->startErrors = {{41, "registration failed"}};
        fake->signalOnStart = true;
        Monitor monitor(std::move(backend));
        auto result = monitor.start();
        REQUIRE(!result.success);
        requireStopped(result.state);
        REQUIRE(result.state.error.code == 41);
        REQUIRE(equal(result.state.error.message, "registration failed"));
        REQUIRE(fake->calls->starts == 1 && fake->calls->stops == 2);
        REQUIRE(monitor.events().count == 1);
        result = monitor.start();
        REQUIRE(result.success && result.state.generation == 2);
        REQUIRE(!result.state.error);
        REQUIRE(result.state.sequence == 2);
    }
    {
        auto backend = std::make_unique<FakeBackend>();
        auto* fake = backend.get();
        fake->stopErrors = {{42, "pre-start cleanup failed"}};
        Monitor monitor(std::move(backend));
        const auto result = monitor.start();
        REQUIRE(!result.success);
        requireStopped(result.state);
        REQUIRE(result.state.error.code == 42);
        REQUIRE(result.state.generation == 0);
        REQUIRE(fake->calls->starts == 0);
        REQUIRE(monitor.start().success);
        REQUIRE(monitor.state().generation == 1);
    }
    {
        auto backend = std::make_unique<FakeBackend>();
        auto* fake = backend.get();
        fake->startErrors = {{43, "start failed"}};
        fake->stopErrors = {{}, {44, "failed-start cleanup failed"}};
        Monitor monitor(std::move(backend));
        const auto result = monitor.start();
        REQUIRE(!result.success);
        requireStopped(result.state);
        REQUIRE(result.state.error.code == 44);
        REQUIRE(equal(result.state.error.message, "failed-start cleanup failed"));
        REQUIRE(monitor.start().success);
    }
    {
        auto backend = std::make_unique<FakeBackend>();
        auto* fake = backend.get();
        fake->stopErrors = {{}, {45, "stop failed"}};
        Monitor monitor(std::move(backend));
        REQUIRE(monitor.start().success);
        const auto result = monitor.stop();
        REQUIRE(!result.success);
        requireStopped(result.state);
        REQUIRE(result.state.error.code == 45);
        REQUIRE(monitor.stop().success);
        REQUIRE(!monitor.state().error);
        REQUIRE(monitor.start().success);
    }
    {
        auto backend = std::make_unique<FakeBackend>();
        auto* fake = backend.get();
        fake->throwOnStart = true;
        Monitor monitor(std::move(backend));
        const auto result = monitor.start();
        REQUIRE(!result.success);
        requireStopped(result.state);
        REQUIRE(result.state.error.code == 10001);
        REQUIRE(equal(result.state.error.message, "Native monitor thread could not start"));
        REQUIRE(fake->calls->stops == 2);
        fake->throwOnStart = false;
        REQUIRE(monitor.start().success);
        REQUIRE(monitor.state().generation == 2);
    }
}

void concurrentMonitor() {
    auto calls = std::make_shared<Calls>();
    {
        auto backend = std::make_unique<FakeBackend>(calls);
        backend->signalOnStart = true;
        Monitor monitor(std::move(backend));
        std::atomic<bool> go{false};
        std::vector<std::thread> threads;
        for (unsigned t = 0; t < 8; ++t) {
            threads.emplace_back([&, t] {
                while (!go.load()) std::this_thread::yield();
                std::uint64_t generation = 0, sequence = 0;
                for (unsigned i = 0; i < 200; ++i) {
                    State state;
                    switch ((i + t) % 4) {
                        case 0: {
                            const auto result = monitor.start();
                            REQUIRE(result.success);
                            state = result.state;
                            REQUIRE(state.running && state.available);
                            break;
                        }
                        case 1: {
                            const auto result = monitor.stop();
                            REQUIRE(result.success);
                            state = result.state;
                            requireStopped(state);
                            break;
                        }
                        case 2: state = monitor.state(); break;
                        default: {
                            const auto batch = monitor.events();
                            REQUIRE(batch.count <= queueCapacity);
                            std::uint64_t last = 0;
                            for (std::size_t j = 0; j < batch.count; ++j) {
                                REQUIRE(batch.events[j].sequence > last);
                                REQUIRE(batch.events[j].generation <= batch.state.generation);
                                last = batch.events[j].sequence;
                            }
                            REQUIRE(last <= batch.state.sequence);
                            state = batch.state;
                            break;
                        }
                    }
                    REQUIRE(state.generation >= generation);
                    REQUIRE(state.sequence >= sequence);
                    REQUIRE(state.running == state.available);
                    REQUIRE(!state.error);
                    if (state.running) REQUIRE(state.lock == Lock::locked);
                    else requireStopped(state);
                    generation = state.generation;
                    sequence = state.sequence;
                }
            });
        }
        go = true;
        for (auto& thread : threads) thread.join();
        REQUIRE(monitor.stop().success);
        REQUIRE(calls->active == 0);
        REQUIRE(calls->starts > 0);
        REQUIRE(monitor.state().generation == calls->starts.load());
    }
    REQUIRE(calls->active == 0);
}

void serialization() {
    REQUIRE(equal(name(Lock::unknown), "unknown"));
    REQUIRE(equal(name(Lock::locked), "locked"));
    REQUIRE(equal(name(Lock::unlocked), "unlocked"));
    const std::string initial =
        "{\"lock\":\"unknown\",\"running\":false,\"available\":false,\"source\":\"none\","
        "\"observedAt\":null,\"generation\":\"0\",\"sequence\":\"0\",\"droppedTotal\":\"0\","
        "\"error\":{\"code\":0,\"message\":\"\"}}";
    REQUIRE(json(State{}) == initial);
    REQUIRE(json(Result{false, {}}) == "{\"success\":false,\"state\":" + initial + "}");
    REQUIRE(json(Batch{}) == "{\"events\":[],\"dropped\":\"0\",\"state\":" + initial + "}");

    State state;
    state.lock = Lock::locked;
    state.running = true;
    state.available = true;
    state.source = "quote\" slash\\\n\t\x01";
    state.observedAt = 1700000000123;
    state.revision = 123; // Internal revision must not leak into the JSON contract.
    state.generation = std::numeric_limits<std::uint64_t>::max();
    state.sequence = 9007199254740993ULL;
    state.droppedTotal = std::numeric_limits<std::uint64_t>::max() - 1;
    state.error = {std::numeric_limits<std::uint32_t>::max(), u8"é\r\b\f"};
    const std::string expected =
        "{\"lock\":\"locked\",\"running\":true,\"available\":true,"
        "\"source\":\"quote\\\" slash\\\\\\u000a\\u0009\\u0001\","
        "\"observedAt\":1700000000123,\"generation\":\"18446744073709551615\","
        "\"sequence\":\"9007199254740993\",\"droppedTotal\":\"18446744073709551614\","
        "\"error\":{\"code\":4294967295,\"message\":\""
        u8"é\\u000d\\u0008\\u000c\"}}";
    REQUIRE(json(state) == expected);
    REQUIRE(json(Result{true, state}) == "{\"success\":true,\"state\":" + expected + "}");
    Batch batch;
    batch.count = 2;
    batch.dropped = std::numeric_limits<std::uint64_t>::max();
    batch.state = state;
    batch.events[0] = {Lock::unknown, Lock::locked, "a\"\\", 123, 9007199254740993ULL, 2};
    batch.events[1] = {Lock::locked, Lock::unlocked, "b\n", 456,
        std::numeric_limits<std::uint64_t>::max(), std::numeric_limits<std::uint64_t>::max()};
    REQUIRE(json(batch) ==
        "{\"events\":[{\"previous\":\"unknown\",\"lock\":\"locked\",\"source\":\"a\\\"\\\\\","
        "\"observedAt\":123,\"sequence\":\"9007199254740993\",\"generation\":\"2\"},"
        "{\"previous\":\"locked\",\"lock\":\"unlocked\",\"source\":\"b\\u000a\","
        "\"observedAt\":456,\"sequence\":\"18446744073709551615\","
        "\"generation\":\"18446744073709551615\"}],\"dropped\":\"18446744073709551615\","
        "\"state\":" + expected + "}");
    state.lock = Lock::unlocked;
    state.running = false;
    state.available = false;
    state.source = "";
    state.observedAt = 0;
    state.generation = state.sequence = state.droppedTotal = 0;
    state.error = {};
    REQUIRE(json(state) ==
        "{\"lock\":\"unlocked\",\"running\":false,\"available\":false,\"source\":\"\","
        "\"observedAt\":null,\"generation\":\"0\",\"sequence\":\"0\",\"droppedTotal\":\"0\","
        "\"error\":{\"code\":0,\"message\":\"\"}}");
}
}

int main() {
    initialAndSnapshots();
    signalsAndFailures();
    fatalBeforeReady();
    overflowAndGenerations();
    concurrentCache();
    monitorLifecycle();
    monitorFailures();
    concurrentMonitor();
    serialization();
    std::cout << "PASS: deterministic cache, monitor, concurrency and serialization tests\n";
}
