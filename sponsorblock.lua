-- TubeView: skips SponsorBlock segments in mpv. tubeview.py looks the segments up and passes them in as
-- --script-opts=tubeview-segments=START:END:CATEGORY:ACTION:MODE;...  (ACTION skip|mute, MODE skip|ask)
local opts = { segments = "" }
require("mp.options").read_options(opts, "tubeview")

local segs = {}
for s, e, cat, act, mode in opts.segments:gmatch("([%d.]+):([%d.]+):([%w_]+):(%a+):(%a+)") do
    segs[#segs + 1] = { s = tonumber(s), e = tonumber(e), cat = (cat:gsub("_", " ")), act = act, mode = mode }
end
if #segs == 0 then return end

local current, sb_muted, user_muted = nil, false, false

-- Tab skips the segment you're in when it's set to "show a skip button"
mp.add_forced_key_binding("TAB", "tubeview-skip", function()
    if current then
        current.done = true
        mp.set_property_number("time-pos", current.e)
        mp.osd_message("Skipped " .. current.cat, 2)
    end
end)

mp.observe_property("time-pos", "number", function(_, t)
    if not t then return end
    local inside, mute = nil, false
    for _, g in ipairs(segs) do
        if t >= g.s and t < g.e - 0.3 then
            if g.act == "mute" then
                mute = true
            elseif not g.done then
                if g.mode == "skip" then
                    g.done = true  -- once: seeking back into it lets you watch it
                    mp.set_property_number("time-pos", g.e)
                    mp.osd_message("Skipped " .. g.cat, 3)
                    mp.msg.info("skipped " .. g.cat .. " " .. g.s .. "-" .. g.e)
                    return
                end
                inside = g
            end
        end
    end
    if inside ~= current then
        current = inside
        if inside then mp.osd_message(inside.cat .. ": press Tab to skip", 4) end
    end
    -- mute only for mute segments, then restore whatever the user had
    if mute and not sb_muted then
        user_muted = mp.get_property_bool("mute")
        mp.set_property_bool("mute", true)
    elseif sb_muted and not mute then
        mp.set_property_bool("mute", user_muted)
    end
    sb_muted = mute
end)
