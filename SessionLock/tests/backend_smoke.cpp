#include "serialization.h"

#include <cstdlib>
#include <cstring>
#include <iostream>
#include <string>
#include <utility>

namespace {
using namespace sessionlock;

[[noreturn]] void requirementFailed(const char* expression, int line) {
    std::cerr << "backend_smoke.cpp:" << line << ": REQUIRE(" << expression << ") failed\n";
    std::abort();
}
#define REQUIRE(expression) ((expression) ? static_cast<void>(0) : requirementFailed(#expression, __LINE__))

void stopped(const State& state) {
    REQUIRE(!state.running && !state.available);
    REQUIRE(state.lock == Lock::unknown);
    REQUIRE(state.observedAt == 0);
    REQUIRE(std::strcmp(state.source, "none") == 0);
}

void stateEnvelope(const State& state) {
    const auto text = json(state);
    REQUIRE(text.front() == '{' && text.back() == '}');
    for (const char* field : {"\"lock\":", "\"running\":", "\"available\":", "\"source\":",
            "\"observedAt\":", "\"generation\":\"", "\"sequence\":\"", "\"droppedTotal\":\"",
            "\"error\":{\"code\":", "\"message\":"})
        REQUIRE(text.find(field) != std::string::npos);
    REQUIRE(!state.available || state.running);
    REQUIRE(state.lock == Lock::unknown || state.lock == Lock::locked || state.lock == Lock::unlocked);
}

void eventEnvelope(const Batch& batch) {
    REQUIRE(batch.count <= queueCapacity);
    stateEnvelope(batch.state);
    const auto text = json(batch);
    REQUIRE(text.find("{\"events\":[") == 0);
    REQUIRE(text.find("],\"dropped\":\"" + std::to_string(batch.dropped) +
        "\",\"state\":" + json(batch.state) + "}") != std::string::npos);
    std::uint64_t last = 0;
    for (std::size_t i = 0; i < batch.count; ++i) {
        const auto& event = batch.events[i];
        REQUIRE(event.sequence > last);
        REQUIRE(event.sequence <= batch.state.sequence);
        REQUIRE(event.generation <= batch.state.generation);
        REQUIRE(event.lock == Lock::locked || event.lock == Lock::unlocked);
        REQUIRE(event.previous != event.lock);
        REQUIRE(event.source != nullptr);
        REQUIRE(event.observedAt != 0);
        last = event.sequence;
    }
}

void diagnose(const char* operation, const Result& result) {
    std::cerr << "Native lifecycle " << operation << " unavailable/failed: code="
              << result.state.error.code << ", message=" << result.state.error.message
              << "; no native monitoring availability or lock integration success is claimed.\n";
}
}

int main() {
    // Only subscribe/query/clean up. Never lock the desktop or synthesize native notifications.
    unsigned successfulStarts = 0, unavailableStarts = 0;
    {
        auto backend = makeBackend();
        REQUIRE(backend != nullptr);
        Monitor monitor(std::move(backend));
        stopped(monitor.state());
        REQUIRE(monitor.state().generation == 0);
        REQUIRE(monitor.state().sequence == 0);
        REQUIRE(!monitor.state().error);
        const auto initial = monitor.events();
        REQUIRE(initial.count == 0 && initial.dropped == 0);
        eventEnvelope(initial);
        std::uint64_t lastGeneration = 0, lastSequence = 0;
        for (unsigned cycle = 0; cycle < 3; ++cycle) {
            const auto started = monitor.start();
            stateEnvelope(started.state);
            REQUIRE(json(started) == std::string("{\"success\":") +
                (started.success ? "true" : "false") + ",\"state\":" + json(started.state) + "}");
            REQUIRE(started.state.generation >= lastGeneration);
            REQUIRE(started.state.sequence >= lastSequence);
            if (started.success) {
                ++successfulStarts;
                REQUIRE(started.state.running && started.state.available);
                const auto repeated = monitor.start();
                if (!repeated.success) {
                    ++unavailableStarts;
                    diagnose("repeated start", repeated);
                    stopped(repeated.state);
                } else {
                    REQUIRE(repeated.state.running && repeated.state.available);
                    REQUIRE(repeated.state.generation == started.state.generation);
                    REQUIRE(repeated.state.sequence >= started.state.sequence);
                }
            } else {
                ++unavailableStarts;
                diagnose("start", started);
                stopped(started.state);
                REQUIRE(static_cast<bool>(started.state.error));
            }
            stateEnvelope(monitor.state());
            eventEnvelope(monitor.events());
            const auto result = monitor.stop();
            if (!result.success) diagnose("stop", result);
            stopped(result.state);
            eventEnvelope(monitor.events());
            const auto repeatedStop = monitor.stop();
            if (!repeatedStop.success) diagnose("repeated stop", repeatedStop);
            stopped(repeatedStop.state);
            REQUIRE(repeatedStop.state.generation == result.state.generation);
            REQUIRE(repeatedStop.state.sequence == result.state.sequence);
            lastGeneration = repeatedStop.state.generation;
            lastSequence = repeatedStop.state.sequence;
        }
        // Exercise destructor cleanup with the native backend running whenever available.
        const auto finalStart = monitor.start();
        if (finalStart.success) {
            ++successfulStarts;
            REQUIRE(finalStart.state.running && finalStart.state.available);
        } else {
            ++unavailableStarts;
            diagnose("destructor-path start", finalStart);
            stopped(finalStart.state);
        }
        eventEnvelope(monitor.events());
    }
    std::cout << "PASS: lifecycle-only smoke checks; available starts=" << successfulStarts
              << ", unavailable starts=" << unavailableStarts
              << ". Desktop locking and lock-event integration were NOT tested.\n";
}
