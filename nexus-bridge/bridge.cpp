// letterbox bridge: a Nexus addon that hands what the game shows addons over to the letterbox bar on
// Linux. It reads Raidcore's RealTime API (RTAPI, its own addon in the Nexus library): game state, your
// character's state and your squad or party with every member's profession, subgroup and tag. Once per
// second it writes that as JSON to addons/letterbox/bridge.json. The game folder is a normal folder on
// Linux under Proton, so the bar simply reads the file.
// Nothing is sent anywhere and nothing in the game is changed or automated.
#include <windows.h>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <map>
#include <string>

#include "Nexus.h"
#include "RTAPI.h"

namespace {

AddonAPI_t* api = nullptr;
RealTimeData* rt = nullptr;
NexusLinkData_t* nexus = nullptr;
CRITICAL_SECTION lock;
std::map<std::string, GroupMember> members;    // by account name, from RTAPI's group events
HANDLE thread = nullptr;
volatile bool running = false;
std::string outPath, tmpPath;

void log(ELogLevel lvl, const char* msg) {
    if (api) api->Log(lvl, "letterbox", msg);
}

// JSON string escaping for names (UTF-8 passes through, control characters are dropped)
std::string esc(const char* s) {
    std::string o = "\"";
    for (; *s; ++s) {
        unsigned char c = (unsigned char)*s;
        if (c == '"' || c == '\\') { o += '\\'; o += (char)c; }
        else if (c >= 0x20) o += (char)c;
    }
    return o + "\"";
}

void onMember(void* payload, bool left) {
    auto* m = (GroupMember*)payload;
    if (!m) return;
    EnterCriticalSection(&lock);
    if (left) members.erase(m->AccountName);
    else members[m->AccountName] = *m;
    LeaveCriticalSection(&lock);
}
void onJoined(void* p) { onMember(p, false); }
void onUpdated(void* p) { onMember(p, false); }
void onLeft(void* p) { onMember(p, true); }

void onAddonLoaded(void* p) {
    if (p && *(uint32_t*)p == RTAPI_SIG) rt = (RealTimeData*)api->DataLink_Get(DL_RTAPI);
}
void onAddonUnloaded(void* p) {
    if (p && *(uint32_t*)p == RTAPI_SIG) {
        rt = nullptr;
        EnterCriticalSection(&lock);
        members.clear();
        LeaveCriticalSection(&lock);
    }
}

std::string snapshot() {
    char buf[512];
    std::string j = "{";
    snprintf(buf, sizeof buf, "\"time\":%lld,\"version\":1", (long long)time(nullptr));
    j += buf;
    if (nexus) {
        snprintf(buf, sizeof buf, ",\"gameplay\":%d,\"moving\":%d,\"camera_moving\":%d",
                 nexus->IsGameplay ? 1 : 0, nexus->IsMoving ? 1 : 0, nexus->IsCameraMoving ? 1 : 0);
        j += buf;
    }
    RealTimeData* r = rt;
    if (!r || r->GameBuild == 0) {             // RTAPI not installed, not loaded, or just unloaded
        j += ",\"rtapi\":false}";
        return j;
    }
    snprintf(buf, sizeof buf,
             ",\"rtapi\":true,\"build\":%u,\"state\":%u,\"map_id\":%u,\"map_type\":%u,\"time_of_day\":%u,"
             "\"server\":\"%u.%u.%u.%u\",\"char_state\":%u,\"level\":%u,\"effective_level\":%u,\"mount\":%u,"
             "\"profession\":%u,\"spec\":%u,\"action_camera\":%u,\"group_type\":%u,\"group_count\":%u",
             r->GameBuild, r->GameState, r->MapID, r->MapType, r->TimeOfDay, r->IPAddress[0], r->IPAddress[1],
             r->IPAddress[2], r->IPAddress[3], r->CharacterState, r->CharacterLevel, r->CharacterEffectiveLevel,
             r->MountIndex, r->Profession, r->EliteSpecialization, (unsigned)r->IsActionCamera, r->GroupType,
             r->GroupMemberCount);
    j += buf;
    j += ",\"account\":" + esc(r->AccountName) + ",\"character\":" + esc(r->CharacterName);
    j += ",\"members\":[";
    EnterCriticalSection(&lock);
    bool first = true;
    for (auto& [name, m] : members) {
        snprintf(buf, sizeof buf, "\"sub\":%u,\"prof\":%u,\"spec\":%u,\"self\":%u,\"here\":%u,\"commander\":%u,"
                 "\"lieutenant\":%u", m.Subgroup, m.Profession, m.EliteSpecialization, (unsigned)m.IsSelf,
                 (unsigned)m.IsInInstance, (unsigned)m.IsCommander, (unsigned)m.IsLieutenant);
        j += std::string(first ? "" : ",") + "{\"account\":" + esc(m.AccountName) + ",\"name\":" +
             esc(m.CharacterName) + "," + buf + "}";
        first = false;
    }
    LeaveCriticalSection(&lock);
    return j + "]}";
}

// once per second: write to a temporary file, then replace bridge.json, so the bar never reads half a file
DWORD WINAPI writer(LPVOID) {
    while (running) {
        std::string j = snapshot();
        if (FILE* f = fopen(tmpPath.c_str(), "wb")) {
            fwrite(j.data(), 1, j.size(), f);
            fclose(f);
            MoveFileExA(tmpPath.c_str(), outPath.c_str(), MOVEFILE_REPLACE_EXISTING);
        }
        for (int i = 0; i < 10 && running; ++i) Sleep(100);
    }
    return 0;
}

void load(AddonAPI_t* a) {
    api = a;
    InitializeCriticalSection(&lock);
    std::string dir = api->Paths_GetAddonDirectory("letterbox");
    CreateDirectoryA(dir.c_str(), nullptr);
    outPath = dir + "\\bridge.json";
    tmpPath = dir + "\\bridge.json.tmp";
    nexus = (NexusLinkData_t*)api->DataLink_Get(DL_NEXUS_LINK);
    rt = (RealTimeData*)api->DataLink_Get(DL_RTAPI);          // nullptr until RTAPI is loaded
    api->Events_Subscribe(EV_ADDON_LOADED, onAddonLoaded);
    api->Events_Subscribe(EV_ADDON_UNLOADED, onAddonUnloaded);
    api->Events_Subscribe(EV_RTAPI_GROUP_MEMBER_JOINED, onJoined);
    api->Events_Subscribe(EV_RTAPI_GROUP_MEMBER_UPDATED, onUpdated);
    api->Events_Subscribe(EV_RTAPI_GROUP_MEMBER_LEFT, onLeft);
    running = true;
    thread = CreateThread(nullptr, 0, writer, nullptr, 0, nullptr);
    log(LOGL_INFO, rt ? "writing bridge.json (RTAPI found)" : "writing bridge.json (RTAPI not loaded yet)");
}

void unload() {
    running = false;
    if (thread) {
        WaitForSingleObject(thread, 2000);
        CloseHandle(thread);
        thread = nullptr;
    }
    api->Events_Unsubscribe(EV_ADDON_LOADED, onAddonLoaded);
    api->Events_Unsubscribe(EV_ADDON_UNLOADED, onAddonUnloaded);
    api->Events_Unsubscribe(EV_RTAPI_GROUP_MEMBER_JOINED, onJoined);
    api->Events_Unsubscribe(EV_RTAPI_GROUP_MEMBER_UPDATED, onUpdated);
    api->Events_Unsubscribe(EV_RTAPI_GROUP_MEMBER_LEFT, onLeft);
    DeleteFileA(outPath.c_str());                              // no stale data for the bar
    DeleteCriticalSection(&lock);
    rt = nullptr;
    nexus = nullptr;
    api = nullptr;
}

AddonDefinition_t def{};

}  // namespace

extern "C" __declspec(dllexport) AddonDefinition_t* GetAddonDef() {
    def.Signature = 0x6C78E8B1;          // random, not a library addon
    def.APIVersion = NEXUS_API_VERSION;
    def.Name = "letterbox bridge";
    def.Version = {1, 0, 0, 0};
    def.Author = "flo-coast";
    def.Description = "Hands squad and game state (from RealTime API) to the letterbox stats bar on Linux.";
    def.Load = load;
    def.Unload = unload;
    def.Flags = AF_None;
    def.Provider = UP_GitHub;
    def.UpdateLink = "https://github.com/flo-coast/letterbox";
    return &def;
}
