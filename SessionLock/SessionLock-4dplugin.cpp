#include "4DPluginAPI.h"
#include "serialization.h"
#include <cstdio>
#include <exception>
#include <vector>

namespace {
sessionlock::Monitor& monitor() {
    static sessionlock::Monitor value(sessionlock::makeBackend());
    return value;
}

void returnJSON(PA_PluginParameters params, const std::string& json) {
    std::vector<PA_Unichar> text(json.begin(), json.end());
    text.push_back(0);
    PA_Unistring string = PA_CreateUnistring(text.data());
    PA_Variable arguments[2]{};
    PA_SetStringVariable(&arguments[0], &string);
    PA_SetLongintVariable(&arguments[1], eVK_Object);
    // The pinned SDK's PA_JsonParse does not initialize its argument variables.
    PA_Variable result = PA_ExecuteCommandByID(1218, arguments, 2);
    PA_ObjectRef object = PA_GetObjectVariable(result);
    if (!object) std::fputs("SessionLock: 4D JSON Parse failed\n", stderr);
    PA_ReturnObject(params, object);
}
}

void PluginMain(PA_long32 selector, PA_PluginParameters params) {
    try {
        switch (selector) {
            case 1: returnJSON(params, sessionlock::json(monitor().start())); break;
            case 2: returnJSON(params, sessionlock::json(monitor().state())); break;
            case 3: returnJSON(params, sessionlock::json(monitor().events())); break;
            case 4: returnJSON(params, sessionlock::json(monitor().stop())); break;
            case kDeinitPlugin:
            case kServerDeinitPlugin: monitor().stop(); break;
            default: break;
        }
    } catch (const std::exception& exception) {
        std::fprintf(stderr, "SessionLock: native command failed: %s\n", exception.what());
        if (selector >= 1 && selector <= 4)
            returnJSON(params, "{\"success\":false,\"error\":{\"code\":10002,\"message\":\"Native command failed; see stderr\"}}");
    }
}
