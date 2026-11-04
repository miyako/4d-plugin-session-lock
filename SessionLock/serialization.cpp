#include "serialization.h"

namespace sessionlock {
namespace {
std::string quote(const char* text) {
    std::string result = "\"";
    for (const unsigned char* p = reinterpret_cast<const unsigned char*>(text); *p; ++p) {
        if (*p == '"' || *p == '\\') result += '\\';
        if (*p < 32) {
            constexpr char hex[] = "0123456789abcdef";
            result += "\\u00";
            result += hex[*p >> 4];
            result += hex[*p & 15];
        } else result += static_cast<char>(*p);
    }
    return result + '"';
}
std::string counter(std::uint64_t value) { return '"' + std::to_string(value) + '"'; }
std::string boolean(bool value) { return value ? "true" : "false"; }
}
std::string json(const State& state) {
    return "{\"lock\":" + quote(name(state.lock)) +
        ",\"running\":" + boolean(state.running) +
        ",\"available\":" + boolean(state.available) +
        ",\"source\":" + quote(state.source) +
        ",\"observedAt\":" + (state.observedAt ? std::to_string(state.observedAt) : "null") +
        ",\"generation\":" + counter(state.generation) +
        ",\"sequence\":" + counter(state.sequence) +
        ",\"droppedTotal\":" + counter(state.droppedTotal) +
        ",\"error\":{\"code\":" + std::to_string(state.error.code) +
        ",\"message\":" + quote(state.error.message) + "}}";
}
std::string json(const Result& result) {
    return "{\"success\":" + boolean(result.success) + ",\"state\":" + json(result.state) + "}";
}
std::string json(const Batch& batch) {
    std::string result = "{\"events\":[";
    for (std::size_t i = 0; i < batch.count; ++i) {
        const auto& event = batch.events[i];
        if (i) result += ',';
        result += "{\"previous\":" + quote(name(event.previous)) +
            ",\"lock\":" + quote(name(event.lock)) +
            ",\"source\":" + quote(event.source) +
            ",\"observedAt\":" + std::to_string(event.observedAt) +
            ",\"sequence\":" + counter(event.sequence) +
            ",\"generation\":" + counter(event.generation) + "}";
    }
    return result + "],\"dropped\":" + counter(batch.dropped) + ",\"state\":" + json(batch.state) + "}";
}
}
