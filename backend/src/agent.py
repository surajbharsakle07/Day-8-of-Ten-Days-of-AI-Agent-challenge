import json
import logging
import os
import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Optional, Annotated

from dotenv import load_dotenv
from pydantic import Field
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    JobProcess,
    RoomInputOptions,
    WorkerOptions,
    cli,
    function_tool,
    RunContext,
)

from livekit.plugins import murf, silero, google, deepgram, noise_cancellation
from livekit.plugins.turn_detector.multilingual import MultilingualModel

# -------------------------
# Logging
# -------------------------
logger = logging.getLogger("voice_game_master")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(handler)

load_dotenv(".env.local")

# -------------------------
# Simple Game World Definition
# -------------------------
# A compact world with a few scenes and choices forming a mini-arc.
WORLD = {
    "intro": {
        "title": "A Shadow over Brinmere",
        "desc": (
            "You awake on the damp shore of Brinmere, the moon a thin silver crescent. "
            "A ruined watchtower smolders a short distance inland, and a narrow path leads "
            "towards a cluster of cottages to the east. In the water beside you lies a "
            "small, carved wooden box, half-buried in sand."
        ),
        "choices": {
            "inspect_box": {
                "desc": "Inspect the carved wooden box at the water's edge.",
                "result_scene": "box",
            },
            "approach_tower": {
                "desc": "Head inland towards the smoldering watchtower.",
                "result_scene": "tower",
            },
            "walk_to_cottages": {
                "desc": "Follow the path east towards the cottages.",
                "result_scene": "cottages",
            },
        },
    },
    "box": {
        "title": "The Box",
        "desc": (
            "The box is warm despite the night air. Inside is a folded scrap of parchment "
            "with a hatch-marked map and the words: 'Beneath the tower, the latch sings.' "
            "As you read, a faint whisper seems to come from the tower, as if the wind "
            "itself speaks your name."
        ),
        "choices": {
            "take_map": {
                "desc": "Take the map and keep it.",
                "result_scene": "tower_approach",
                "effects": {"add_journal": "Found map fragment: 'Beneath the tower, the latch sings.'"},
            },
            "leave_box": {
                "desc": "Leave the box where it is.",
                "result_scene": "intro",
            },
        },
    },
    "tower": {
        "title": "The Watchtower",
        "desc": (
            "The watchtower's stonework is cracked and warm embers glow within. An iron "
            "latch covers a hatch at the base — it looks old but recently used. You can "
            "try the latch, look for other entrances, or retreat."
        ),
        "choices": {
            "try_latch_without_map": {
                "desc": "Try the iron latch without any clue.",
                "result_scene": "latch_fail",
            },
            "search_around": {
                "desc": "Search the nearby rubble for another entrance.",
                "result_scene": "secret_entrance",
            },
            "retreat": {
                "desc": "Step back to the shoreline.",
                "result_scene": "intro",
            },
        },
    },
    "tower_approach": {
        "title": "Toward the Tower",
        "desc": (
            "Clutching the map, you approach the watchtower. The map's marks align with "
            "the hatch at the base, and you notice a faint singing resonance when you step close."
        ),
        "choices": {
            "open_hatch": {
                "desc": "Use the map clue and try the hatch latch carefully.",
                "result_scene": "latch_open",
                "effects": {"add_journal": "Used map clue to open the hatch."},
            },
            "search_around": {
                "desc": "Search for another entrance.",
                "result_scene": "secret_entrance",
            },
            "retreat": {
                "desc": "Return to the shore.",
                "result_scene": "intro",
            },
        },
    },
    "latch_fail": {
        "title": "A Bad Twist",
        "desc": (
            "You twist the latch without heed — the mechanism sticks, and the effort sends "
            "a shiver through the ground. From inside the tower, something rustles in alarm."
        ),
        "choices": {
            "run_away": {
                "desc": "Run back to the shore.",
                "result_scene": "intro",
            },
            "stand_ground": {
                "desc": "Stand and prepare for whatever emerges.",
                "result_scene": "tower_combat",
            },
        },
    },
    "latch_open": {
        "title": "The Hatch Opens",
        "desc": (
            "With the map's guidance the latch yields and the hatch opens with a breath of cold air. "
            "Inside, a spiral of rough steps leads down into an ancient cellar lit by phosphorescent moss."
        ),
        "choices": {
            "descend": {
                "desc": "Descend into the cellar.",
                "result_scene": "cellar",
            },
            "close_hatch": {
                "desc": "Close the hatch and reconsider.",
                "result_scene": "tower_approach",
            },
        },
    },
    "secret_entrance": {
        "title": "A Narrow Gap",
        "desc": (
            "Behind a pile of rubble you find a narrow gap and old rope leading downward. "
            "It smells of cold iron and something briny."
        ),
        "choices": {
            "squeeze_in": {
                "desc": "Squeeze through the gap and follow the rope down.",
                "result_scene": "cellar",
            },
            "mark_and_return": {
                "desc": "Mark the spot and return to the shore.",
                "result_scene": "intro",
            },
        },
    },
    "cellar": {
        "title": "Cellar of Echoes",
        "desc": (
            "The cellar opens into a circular chamber where runes glow faintly. At the center "
            "is a stone plinth and upon it a small brass key and a sealed scroll."
        ),
        "choices": {
            "take_key": {
                "desc": "Pick up the brass key.",
                "result_scene": "cellar_key",
                "effects": {"add_inventory": "brass_key", "add_journal": "Found brass key on plinth."},
            },
            "open_scroll": {
                "desc": "Break the seal and read the scroll.",
                "result_scene": "scroll_reveal",
                "effects": {"add_journal": "Scroll reads: 'The tide remembers what the villagers forget.'"},
            },
            "leave_quietly": {
                "desc": "Leave the cellar and close the hatch behind you.",
                "result_scene": "intro",
            },
        },
    },
    "cellar_key": {
        "title": "Key in Hand",
        "desc": (
            "With the key in your hand the runes dim and a hidden panel slides open, revealing a "
            "small statue that begins to hum. A voice, ancient and kind, asks: 'Will you return what was taken?'"
        ),
        "choices": {
            "pledge_help": {
                "desc": "Pledge to return what was taken.",
                "result_scene": "reward",
                "effects": {"add_journal": "You pledged to return what was taken."},
            },
            "refuse": {
                "desc": "Refuse and pocket the key.",
                "result_scene": "cursed_key",
                "effects": {"add_journal": "You pocketed the key; a weight grows in your pocket."},
            },
        },
    },
    "scroll_reveal": {
        "title": "The Scroll",
        "desc": (
            "The scroll tells of an heirloom taken by a water spirit that dwells beneath the tower. "
            "It hints that the brass key 'speaks' when offered with truth."
        ),
        "choices": {
            "search_for_key": {
                "desc": "Search the plinth for a key.",
                "result_scene": "cellar_key",
            },
            "leave_quietly": {
                "desc": "Leave the cellar and keep the knowledge to yourself.",
                "result_scene": "intro",
            },
        },
    },
    "tower_combat": {
        "title": "Something Emerges",
        "desc": (
            "A hunched, brine-soaked creature scrambles out from the tower. Its eyes glow with hunger. "
            "You must act quickly."
        ),
        "choices": {
            "fight": {
                "desc": "Fight the creature.",
                "result_scene": "fight_win",
            },
            "flee": {
                "desc": "Flee back to the shore.",
                "result_scene": "intro",
            },
        },
    },
    "fight_win": {
        "title": "After the Scuffle",
        "desc": (
            "You manage to fend off the creature; it flees wailing towards the sea. On the ground lies "
            "a small locket engraved with a crest — likely the heirloom mentioned in the scroll."
        ),
        "choices": {
            "take_locket": {
                "desc": "Take the locket and examine it.",
                "result_scene": "reward",
                "effects": {"add_inventory": "engraved_locket", "add_journal": "Recovered an engraved locket."},
            },
            "leave_locket": {
                "desc": "Leave the locket and tend to your wounds.",
                "result_scene": "intro",
            },
        },
    },
    "reward": {
        "title": "A Minor Resolution",
        "desc": (
            "A small sense of peace settles over Brinmere. Villagers may one day know the heirloom is found, or it may remain a secret. "
            "You feel the night shift; the little arc of your story here closes for now."
        ),
        "choices": {
            "end_session": {
                "desc": "End the session and return to the shore (conclude mini-arc).",
                "result_scene": "intro",
            },
            "keep_exploring": {
                "desc": "Keep exploring for more mysteries.",
                "result_scene": "intro",
            },
        },
    },
    "cursed_key": {
        "title": "A Weight in the Pocket",
        "desc": (
            "The brass key glows coldly. You feel a heavy sorrow that tugs at your thoughts. "
            "Perhaps the key demands something in return..."
        ),
        "choices": {
            "seek_redemption": {
                "desc": "Seek a way to make amends.",
                "result_scene": "reward",
            },
            "bury_key": {
                "desc": "Bury the key and hope the weight fades.",
                "result_scene": "intro",
            },
        },
    },
    "cottages": {
        "title": "The Sleeping Village",
        "desc": (
            "The cluster of cottages is dark and quiet. A single, small candle flickers in one window. "
            "You can approach the lit window, knock on a door, or return to the shore."
        ),
        "choices": {
            "approach_window": {
                "desc": "Approach the window with the light.",
                "result_scene": "window_check",
            },
            "knock_door": {
                "desc": "Knock on a nearby door.",
                "result_scene": "door_knock",
            },
            "return_to_shore": {
                "desc": "Return to the shore.",
                "result_scene": "intro",
            },
        },
    },
    "window_check": {
        "title": "The Faint Light",
        "desc": (
            "The window is small and grimy. You see an old woman, Elara, hunched over a spinning wheel, "
            "her face etched with worry. She does not seem to have noticed you."
        ),
        "choices": {
            "hail_elara": {
                "desc": "Call out to Elara.",
                "result_scene": "elara_talk",
                "effects": {"add_journal": "Met Elara, an old woman in the cottages."},
            },
            "move_on": {
                "desc": "Move away quietly.",
                "result_scene": "cottages",
            },
        },
    },
    "elara_talk": {
        "title": "Elara's Sorrow",
        "desc": (
            "Elara turns, startled. Her eyes are watery but sharp. 'The tower fire was ill luck,' she whispers. "
            "'They say the sea claims more than just ships. If you seek the truth of Brinmere, look to the tide mark, not the stone.'"
        ),
        "choices": {
            "ask_about_tide_mark": {
                "desc": "Ask Elara what she means by the 'tide mark'.",
                "result_scene": "elara_clue",
            },
            "thank_and_leave": {
                "desc": "Thank her and leave.",
                "result_scene": "cottages",
            },
        },
    },
    "elara_clue": {
        "title": "A Cryptic Hint",
        "desc": (
            "Elara shakes her head. 'The tide mark is the edge of memory. Go back to where you woke and look deeper at the sand.' "
            "She then closes her eyes, refusing to speak further."
        ),
        "choices": {
            "return_to_shore": {
                "desc": "Return immediately to the shore.",
                "result_scene": "intro",
            },
            "try_another_door": {
                "desc": "Try to talk to another villager.",
                "result_scene": "door_knock",
            },
        },
    },
    "door_knock": {
        "title": "Unanswered Door",
        "desc": (
            "You knock gently on a cottage door. There is no answer, only a deep silence from within. "
            "The villagers here seem to keep to themselves."
        ),
        "choices": {
            "try_another_door": {
                "desc": "Knock on another door.",
                "result_scene": "door_knock",
            },
            "return_to_shore": {
                "desc": "Return to the shore.",
                "result_scene": "intro",
            },
        },
    },
}

# -------------------------
# Per-session Userdata
# -------------------------
@dataclass
class Userdata:
    player_name: Optional[str] = None
    current_scene: str = "intro"
    history: List[Dict] = field(default_factory=list)  # list of {'scene', 'action', 'time', 'result_scene'}
    journal: List[str] = field(default_factory=list)
    inventory: List[str] = field(default_factory=list)
    named_npcs: Dict[str, str] = field(default_factory=dict)
    choices_made: List[str] = field(default_factory=list)
    session_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    started_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")

# -------------------------
# Helper functions
# -------------------------
def scene_text(scene_key: str, userdata: Userdata) -> str:
    """
    Build the descriptive text for the current scene, and append choices as short hints.
    Always end with 'What do you do?' so the voice flow prompts player input.
    """
    scene = WORLD.get(scene_key)
    if not scene:
        return "You are in a featureless void. What do you do?"

    # Check for inventory/journal items to subtly change the description (optional, but good)
    if scene_key == "tower" and "Found map fragment: 'Beneath the tower, the latch sings.'" in userdata.journal:
        # If the player has the map, transition them to the better scene instantly, or hint strongly
        return scene_text("tower_approach", userdata)


    desc = f"{scene['desc']}\n\nChoices:\n"
    for cid, cmeta in scene.get("choices", {}).items():
        desc += f"- {cmeta['desc']}\n" 
    
    # GM MUST end with the action prompt
    desc += "\nWhat do you do?"
    return desc

def apply_effects(effects: dict, userdata: Userdata):
    if not effects:
        return
    if "add_journal" in effects:
        userdata.journal.append(effects["add_journal"])
    if "add_inventory" in effects:
        # Only add if not already present
        if effects["add_inventory"] not in userdata.inventory:
            userdata.inventory.append(effects["add_inventory"])
    # Extendable for more effect keys

def summarize_scene_transition(old_scene: str, action_key: str, result_scene: str, userdata: Userdata) -> str:
    """Record the transition into history and return a short narrative the GM can use."""
    entry = {
        "from": old_scene,
        "action": action_key,
        "to": result_scene,
        "time": datetime.utcnow().isoformat() + "Z",
    }
    userdata.history.append(entry)
    userdata.choices_made.append(action_key)
    # Use the description of the choice for a more narrative response
    choice_desc = WORLD[old_scene]['choices'][action_key]['desc']
    
    # Add a slight narrative flourish
    if 'take' in action_key or 'pick' in action_key:
        return f"You {choice_desc.lower().replace('up the', '').replace('the', '')}, a new path opens."
    if 'approach' in action_key or 'go to' in action_key or 'walk' in action_key:
        return f"You decide to {choice_desc.lower().replace('go to', '').replace('head', '')} and proceed."
    
    return f"You chose to '{choice_desc.strip('.')}', and the scene changes."

# -------------------------
# Agent Tools (function_tool)
# -------------------------

@function_tool
async def start_adventure(
    ctx: RunContext[Userdata],
    player_name: Annotated[Optional[str], Field(description="Player name", default=None)] = None,
) -> str:
    """Initialize a new adventure session for the player and return the opening description."""
    userdata = ctx.userdata
    if player_name:
        userdata.player_name = player_name
    userdata.current_scene = "intro"
    userdata.history = []
    userdata.journal = []
    userdata.inventory = []
    userdata.named_npcs = {}
    userdata.choices_made = []
    userdata.session_id = str(uuid.uuid4())[:8]
    userdata.started_at = datetime.utcnow().isoformat() + "Z"

    opening = (
        f"Greetings {userdata.player_name or 'traveler'}. Welcome to '{WORLD['intro']['title']}'. "
        "A new adventure begins now.\n\n"
        + scene_text("intro", userdata)
    )
    # Ensure GM prompt present
    if not opening.endswith("What do you do?"):
        opening += "\nWhat do you do?"
    return opening

@function_tool
async def get_scene(
    ctx: RunContext[Userdata],
) -> str:
    """Return the current scene description (useful for 'remind me where I am')."""
    userdata = ctx.userdata
    scene_k = userdata.current_scene or "intro"
    txt = scene_text(scene_k, userdata)
    return txt

@function_tool
async def player_action(
    ctx: RunContext[Userdata],
    action: Annotated[str, Field(description="Player spoken action (e.g., 'I want to check the wooden box' or 'fight the monster')")],
) -> str:
    """
    Accept player's action, use advanced logic to resolve it to a defined choice,
    update userdata, advance to the next scene and return the GM's next description (ending with 'What do you do?').
    """
    userdata = ctx.userdata
    current = userdata.current_scene or "intro"
    scene = WORLD.get(current)
    action_text = (action or "").strip()
    chosen_key = None

    if not scene:
        # Should not happen in this structured world, but a safe fallback
        return restart_adventure(ctx)

    # --- Action Resolution: Attempt 1, 2, 3 (Fuzzy Match/Keyword) ---

    # Attempt 1: match exact action key (e.g., 'inspect_box')
    if action_text.lower() in (scene.get("choices") or {}):
        chosen_key = action_text.lower()
    
    # Attempt 2: fuzzy match by checking if action_text contains the choice key or descriptive words
    if not chosen_key:
        for cid, cmeta in (scene.get("choices") or {}).items():
            desc = cmeta.get("desc", "").lower()
            # Look for exact key or a match on a few descriptive keywords
            if cid in action_text.lower() or any(w in action_text.lower() for w in desc.lower().split()[:4] if len(w) > 2):
                chosen_key = cid
                break

    # Attempt 3: secondary keyword matching
    if not chosen_key:
        for cid, cmeta in (scene.get("choices") or {}).items():
            desc = cmeta.get("desc", "").lower()
            for keyword in ["take", "pick", "open", "go", "return", "leave", "fight", "flee", "search", "descend", "close"]:
                if keyword in action_text.lower() and keyword in desc:
                    chosen_key = cid
                    break
            if chosen_key:
                break


    # --- Action Resolution: Attempt 4 (LLM Interpretation) ---
    if not chosen_key:
        # Use LLM to resolve natural language action to a specific choice key
        choice_map = {cid: cmeta['desc'] for cid, cmeta in (scene.get("choices") or {}).items()}
        choices_prompt = json.dumps(choice_map, indent=2)

        resolution_prompt = f"""
        The player is currently in the scene: '{scene['title']}'.
        The available choice keys and their descriptions are:
        {choices_prompt}
        The player's spoken action was: '{action_text}'.
        
        Your task is to act as a logic engine. Resolve the player's action to the single best matching choice key from the list.
        Do not output any explanation, quotation marks, or extra text.
        If the action is ambiguous, irrelevant, or does not clearly map to any choice, output the word 'NONE'.
        Output ONLY the choice key or 'NONE'.
        """
        try:
            llm_result = await ctx.llm.chat_completion(
                messages=[{"role": "user", "content": resolution_prompt}],
                model="gemini-2.5-flash", # Use a fast model for resolution
                temperature=0.0
            )
            # Clean the output to ensure it is just the key
            resolved_key = llm_result.choices[0].message.content.strip().lower().replace('"', '').replace("'", "")
            if resolved_key in choice_map:
                chosen_key = resolved_key
                logger.info(f"LLM resolved action '{action_text}' to key: {chosen_key}")
            else:
                logger.info(f"LLM could not resolve action: '{action_text}'. Result: {resolved_key}")

        except Exception as e:
            logger.error(f"LLM resolution failed during tool execution: {e}")
            # Fall back to clarification message if LLM fails

    if not chosen_key:
        # Final fallback: If we still can't resolve, ask for clarification
        resp = (
            "I need a clearer action to move the story forward. Please choose one of the options I presented, "
            "or use a very simple phrase related to the options, like 'take the map' or 'descend'.\n\n"
            + scene_text(current, userdata)
        )
        return resp

    # --- Execute Action ---
    
    # Get the chosen choice's metadata
    choice_meta = scene["choices"].get(chosen_key)
    result_scene = choice_meta.get("result_scene", current)
    effects = choice_meta.get("effects", None)

    # Apply effects (inventory/journal, etc.)
    apply_effects(effects or {}, userdata)

    # Record transition and get narrative summary
    _note = summarize_scene_transition(current, chosen_key, result_scene, userdata)

    # Update current scene
    userdata.current_scene = result_scene

    # Build narrative reply: echo a short confirmation, then describe next scene
    next_desc = scene_text(result_scene, userdata)

    # Removed the specific persona prefix ("Aurek, the Game Master, says: ")
    reply = f"{_note}\n\n{next_desc}"
    
    # ensure final prompt present
    if not reply.endswith("What do you do?"):
        reply += "\nWhat do you do?"
    return reply

@function_tool
async def show_journal(
    ctx: RunContext[Userdata],
) -> str:
    userdata = ctx.userdata
    lines = []
    lines.append(f"Session: {userdata.session_id} | Started at: {userdata.started_at}")
    if userdata.player_name:
        lines.append(f"Player: {userdata.player_name}")
    if userdata.journal:
        lines.append("\nJournal entries:")
        for j in userdata.journal:
            lines.append(f"- {j}")
    else:
        lines.append("\nJournal is empty.")
    if userdata.inventory:
        lines.append("\nInventory:")
        for it in userdata.inventory:
            lines.append(f"- {it}")
    else:
        lines.append("\nNo items in inventory.")
    lines.append("\nRecent choices:")
    for h in userdata.history[-6:]:
        lines.append(f"- {h['time']} | from {h['from']} -> {h['to']} via {h['action']}")
    lines.append("\nWhat do you do?")
    return "\n".join(lines)

@function_tool
async def restart_adventure(
    ctx: RunContext[Userdata],
) -> str:
    """Reset the userdata and start again."""
    userdata = ctx.userdata
    userdata.current_scene = "intro"
    userdata.history = []
    userdata.journal = []
    userdata.inventory = []
    userdata.named_npcs = {}
    userdata.choices_made = []
    userdata.session_id = str(uuid.uuid4())[:8]
    userdata.started_at = datetime.utcnow().isoformat() + "Z"
    greeting = (
        "The world resets. A new tide laps at the shore. You stand once more at the beginning.\n\n"
        + scene_text("intro", userdata)
    )
    if not greeting.endswith("What do you do?"):
        greeting += "\nWhat do you do?"
    return greeting

# -------------------------
# The Agent (GameMasterAgent)
# -------------------------
class GameMasterAgent(Agent):
    def __init__(self):
        # System instructions define Universe, Tone, Role
        instructions = """
        You are the Game Master (GM) for a voice-only, Dungeons-and-Dragons-style short adventure.
        Universe: Low-magic coastal fantasy (village of Brinmere, tide-smoothed ruins, minor spirits).
        Tone: Mysterious, dramatic, empathetic.
        Role: You are the GM. You describe scenes vividly, remember the player's past choices, named NPCs, inventory and locations.
              You must use your internal intelligence to interpret the player's natural language action and select the most appropriate
              game tool to advance the story.
        Rules:
            - Use the provided tools to start the adventure, get the current scene, accept the player's spoken action,
              show the player's journal, or restart the adventure.
            - When using the 'player_action' tool, pass the player's full spoken input. The tool has internal logic, assisted by the LLM, 
              to resolve complex natural language input into one of the scene's defined choices.
            - Keep continuity using the per-session userdata. Reference journal items and inventory when relevant.
            - Drive short sessions (aim for several meaningful turns). Each GM message MUST end with 'What do you do?'.
            - Respect that this agent is voice-first: responses should be concise enough for spoken delivery but evocative.
        """
        super().__init__(
            instructions=instructions,
            tools=[start_adventure, get_scene, player_action, show_journal, restart_adventure],
        )

# -------------------------
# Entrypoint & Prewarm (keeps speech functionality)
# -------------------------
def prewarm(proc: JobProcess):
    # load VAD model and stash on process userdata, try/catch like original file
    try:
        proc.userdata["vad"] = silero.VAD.load()
    except Exception:
        logger.warning("VAD prewarm failed; continuing without preloaded VAD. (This may be expected in some environments.)")

async def entrypoint(ctx: JobContext):
    ctx.log_context_fields = {"room": ctx.room.name}
    logger.info("Starting Voice Game Master Agent")

    userdata = Userdata()

    session = AgentSession(
        stt=deepgram.STT(model="nova-3"),
        llm=google.LLM(model="gemini-2.5-flash"),
        tts=murf.TTS(
            voice="en-US-marcus",
            style="Conversational",
            text_pacing=True,
        ),
        turn_detection=MultilingualModel(),
        vad=ctx.proc.userdata.get("vad"),
        userdata=userdata,
    )

    # Start the agent session with the GameMasterAgent
    await session.start(
        agent=GameMasterAgent(),
        room=ctx.room,
        room_input_options=RoomInputOptions(noise_cancellation=noise_cancellation.BVC()),
    )

    await ctx.connect()

if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))