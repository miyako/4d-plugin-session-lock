#pragma once
#include "core.h"
#include <memory>

namespace sessionlock {
class Backend {
public:
    virtual ~Backend() = default;
    // Called serially by Monitor, never with the cache mutex held.
    // start waits for registration/snapshot readiness; stop joins all native work.
    virtual Error start(Cache& cache) = 0;
    virtual Error stop() = 0;
};
std::unique_ptr<Backend> makeBackend();
}
