-- When an episode/movie stops on the TV, tell the HTPC website where it stopped. The website saves that to
-- Jellyfin, so "Continue watching" on phones/PCs (and the website) picks up from the TV's spot.
local function report()
    local path = mp.get_property("path")
    local pos = mp.get_property_number("time-pos")
    local dur = mp.get_property_number("duration")
    if not path or not pos or not dur or dur < 300 or path:sub(1, 1) ~= "/" then return end
    mp.command_native({name = "subprocess", playback_only = false, capture_stdout = true, capture_stderr = true,
        args = {"curl", "-s", "-m", "4", "--data-urlencode", "path=" .. path, "--data-urlencode", "pos=" .. string.format("%.1f", pos),
                "--data-urlencode", "dur=" .. string.format("%.1f", dur), "http://127.0.0.1:5050/api/mpv-progress"}})
end
mp.add_hook("on_unload", 50, report)
