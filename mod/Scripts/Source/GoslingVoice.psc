Scriptname GoslingVoice Hidden
{Gosling's voice from his own mouth. Called by the SkyrimCompanion SKSE plugin.
The companion service writes the line's .wav into Sound\Voice\Gosling.esp\GoslingVoice\
right before the call; the NPC then says the placeholder topic like any other line.}

; Implemented in the SKSE plugin: writes to SkyrimCompanion.log.
Function Log(string asMessage) global native

; Implemented in the SKSE plugin: switch the speaker (and his template) to the GoslingVoice voice type
; and back - like Mantella's SetVoiceType. His normal lines keep Erik's MaleYoungEager voice.
Function UseOwnVoice(Actor akActor, bool abOwn) global native

Function SayLine(Actor akSpeaker, Topic akTopic) global
	Log("SayLine: speaker=" + akSpeaker + " topic=" + akTopic)
	If !akSpeaker || !akTopic
		Log("ERROR: speaker or topic is None")
		Return
	EndIf
	; Placeholder topics belong to GoslingVQ: if the quest is not running, Say() is silently ignored.
	; It is start-game-enabled, but Gosling.esp may be added to an existing save - make sure it runs.
	Quest voiceQuest = Game.GetFormFromFile(0x803, "Gosling.esp") as Quest
	If !voiceQuest
		Log("ERROR: GoslingVQ not found in Gosling.esp")
	ElseIf !voiceQuest.IsRunning()
		Log("GoslingVQ is not running - starting it")
		voiceQuest.Start()
	EndIf
	UseOwnVoice(akSpeaker, true)
	akSpeaker.Say(akTopic)
	; The voice file may be looked up when the line starts, not at the call - keep our voice type a bit.
	Utility.Wait(0.5)
	UseOwnVoice(akSpeaker, false)
	Log("Say() done; in dialogue with player: " + akSpeaker.IsInDialogueWithPlayer() + ", 3D loaded: " + akSpeaker.Is3DLoaded())
EndFunction
