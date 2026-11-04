#pragma once
#include "monitor.h"
#include <string>
namespace sessionlock {
std::string json(const State& state);
std::string json(const Batch& batch);
std::string json(const Result& result);
}
