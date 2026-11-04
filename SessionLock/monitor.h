#pragma once
#include "backend.h"

namespace sessionlock {
struct Result { bool success; State state; };
class Monitor {
public:
    explicit Monitor(std::unique_ptr<Backend> backend);
    ~Monitor();
    Result start();
    Result stop();
    State state();
    Batch events();
private:
    std::mutex lifecycle_;
    Cache cache_;
    std::unique_ptr<Backend> backend_;
};
}
