// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * Robinauts' application, in the area oneberth's shell gives it: the panel on
 * the left, the chat in the middle, the side panel on the right, and no top
 * bar (`docs/specs/frontend.md`).
 *
 * The rail of applications, the theme toggle, the account and the sign-in
 * page are the shell's. The panel is ours, and goes where the shell says
 * (`ShellPanel`): beside the page on a wide screen, where it collapses to a
 * rail of icons remembered per browser, and in the shell's drawer on a small
 * one.
 *
 * **Hidden, it stops its long poll** (oneberth's `shell.md`): the change feed
 * waits while neorc is shown, and reads again as soon as Robinauts is shown.
 *
 * The shape is neorc's, written again for this project; recorded in
 * `docs/legal/ip-clearance.md`.
 */
import { ShellPanel } from "@oneberth/ui";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Chat, type Drafts } from "../chat";
import { useChanges } from "../changes/changes";
import { conversationIn, useHistory } from "../history/history";
import { PanelRightIcon } from "../icons";
import { NEW_CHAT, useRouter, type Route } from "../router";
import { shownTitle } from "../conversation/conversation";
import { AppTab } from "../side/AppTab";
import { appOf, appOffers, toolResults, useApps } from "../side/apps";
import { SIDE_ID, SidePanel } from "../side/SidePanel";
import { carryOpenTabs, NEW_CHAT_TABS, type TabRenderers } from "../side/tabs";
import type { Conversation } from "../conversation/conversation";
import {
  AgentPicker,
  type Agents,
  useAgents,
  useChosenAgent,
} from "./AgentPicker";
import {
  ConversationModel,
  ModelPicker,
  modelsOf,
  useChosenModel,
  useModels,
} from "./ModelPicker";
import { Panel } from "./Panel";
import { SearchPage } from "./SearchPage";
import { ProjectPicker } from "./ProjectPicker";
import {
  createProject,
  useChosenProject,
  useFolded,
  useProjects,
} from "../projects/projects";
import { remember, remembered } from "./storage";
import "./shell.css";

export const PANEL_KEY = "panel";

/**
 * What to call the agent a conversation is with (`docs/specs/agents.md`).
 *
 * The conversation names its agent by id, and the list the picker was given
 * has the title. Until that list has arrived there is nothing to call it by,
 * so nothing is said rather than an id that a moment later turns into a
 * name. An id the list does not have -- an agent the operator has removed,
 * or a list that never came -- is shown as it is: a conversation with an
 * agent nobody can name any more still had one.
 */
export function agentTitle(agents: Agents, id: string): string | null {
  if (agents.status === "loading") return null;
  if (agents.status === "failed") return id;
  return agents.items.find((agent) => agent.id === id)?.title ?? id;
}

/** The models that agent lists; `null` while the agents are not known. */
function listedModels(agents: Agents, id: string | null): string[] | null {
  if (agents.status !== "ready" || id === null) return null;
  return agents.items.find((agent) => agent.id === id)?.models ?? null;
}

/** What the shell holds about the conversation on the screen. */
interface Opened {
  id: string;
  agent: string;
  title: string | null;
  model: string | null;
  /** When it was last written to, as the answer said; `null` if unknown. */
  updatedAt: string | null;
}

function openedFrom(conversation: Conversation): Opened {
  return {
    id: conversation.id,
    agent: conversation.agent,
    title: conversation.title,
    model: conversation.model,
    updatedAt: conversation.updated_at,
  };
}

/**
 * The later of two answers about the same conversation.
 *
 * Answers arrive in the order the network hands them over, not the order
 * the server wrote them: a read of the conversation answered just before a
 * model change was saved can land just after that change's own answer, and
 * would put the old model back on the screen. Every write dates the
 * conversation (`docs/specs/conversations.md`), so the one written later is
 * the one to keep. A date nobody has -- a conversation this page has just
 * started -- is older than any date an answer carries; a tie, or two with
 * none, goes to what arrived last.
 */
function later(was: Opened | null, next: Opened): Opened {
  if (was?.id !== next.id || was.updatedAt === null) return next;
  if (next.updatedAt === null) return was;
  return compareInstants(was.updatedAt, next.updatedAt) > 0 ? was : next;
}

/**
 * Two of the server's instants in order, to the digit it wrote.
 *
 * `Date.parse` keeps milliseconds and the server writes microseconds, so a
 * read and a change a few microseconds apart would be taken for a tie. The
 * fraction is compared as digits beside the milliseconds the rest parses to.
 */
function compareInstants(a: string, b: string): number {
  const [aMs, aFraction] = instant(a);
  const [bMs, bFraction] = instant(b);
  if (aMs !== bMs) return aMs < bMs ? -1 : 1;
  return aFraction < bFraction ? -1 : aFraction > bFraction ? 1 : 0;
}

function instant(text: string): [number, string] {
  const found =
    /^([^T]*T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})?$/.exec(text);
  if (found === null) return [Date.parse(text), ""];
  const [, whole = "", fraction = "", zone = ""] = found;
  return [Date.parse(whole + zone), fraction.padEnd(9, "0")];
}

/**
 * What the shell holds about the conversation on the screen: the later of
 * the panel's row and the chat's own read, by `updated_at` (`later`), the
 * chat's on a tie.
 *
 * The model is changed here and not in the panel, so the chat's read -- or
 * the change's own answer -- is usually the fresher; but not always: another
 * tab may have moved it, and the panel's next page says so. A picker showing
 * the older model would send nothing when the newer one's predecessor is
 * picked, taking it to be current, and the next turn would run on the other.
 */
function freshest(listed: Opened | null, about: Opened | null): Opened | null {
  if (listed === null) return about;
  if (about === null) return listed;
  return later(listed, about);
}

function sameRoute(a: Route, b: Route): boolean {
  return (
    a.kind === b.kind &&
    (a.kind !== "conversation" || a.id === (b as typeof a).id)
  );
}

/** The rail, as this browser last left it. */
function usePanelCollapsed(): [boolean, (collapsed: boolean) => void] {
  const [collapsed, setCollapsed] = useState(
    () => remembered(PANEL_KEY) === "rail",
  );
  return [
    collapsed,
    (next: boolean) => {
      setCollapsed(next);
      remember(PANEL_KEY, next ? "rail" : "open");
    },
  ];
}

export function Workspace({
  visible = true,
}: Readonly<{
  /** Whether the shell shows this application: hidden, it polls nothing. */
  visible?: boolean;
}>) {
  const [collapsed, setCollapsed] = usePanelCollapsed();
  // Where the application is: the empty chat or a conversation
  // (`src/router.ts`).
  const { route, navigate } = useRouter();
  const history = useHistory();
  // Something of this user changed in another tab or client: the list is
  // asked for again, and the chat reads its conversation again
  // (`../changes/changes.ts`).
  const [changes, setChanges] = useState(0);
  const refreshHistory = history.refresh;
  const changed = useCallback(() => {
    refreshHistory();
    setChanges((count) => count + 1);
  }, [refreshHistory]);
  // The projects, asked for again with everything else that changes, and
  // after one is made here; the one a first message goes in; and which are
  // folded away in the panel (`../projects/projects.ts`).
  const [projectsRound, setProjectsRound] = useState(0);
  const projects = useProjects(changes + projectsRound);
  const [projectId, chooseProject] = useChosenProject(projects);
  const folded = useFolded();
  const newProject = useCallback(
    async (name: string) => {
      const made = await createProject(name);
      chooseProject(made.id);
      setProjectsRound((round) => round + 1);
    },
    [chooseProject],
  );
  useChanges(changed, visible);
  // "New chat" pressed while the empty chat is already up changes no hash
  // and so re-renders nothing; the count is what remounts the area, so
  // nothing typed into it carries over into the next one.
  const [chat, setChat] = useState(0);
  // What was written and not sent in each conversation, kept across those
  // remounts, and for as long as somebody is signed in: this shell is
  // unmounted when they sign out, and the drafts go with it.
  const [drafts] = useState<Drafts>(() => new Map());
  // Asked for here, not inside the empty chat: that is remounted on every
  // "New chat", and the agents do not change while the server is running.
  // The choice is held here too, because the chat needs it to begin a
  // conversation and the picker is what the chat draws above its box.
  const agents = useAgents();
  const [agentId, chooseAgent] = useChosenAgent(agents);
  // The models likewise, and the one a first message runs on: the chosen
  // agent's default until somebody picks another (`./ModelPicker.tsx`).
  // Asked for again when the backend refuses a model the list offered: the
  // list is stale, and a picker still offering that model would have every
  // send refused for it until a reload.
  const [modelsRound, setModelsRound] = useState(0);
  const models = useModels(modelsRound);
  const modelsStale = useCallback(() => {
    setModelsRound((round) => round + 1);
  }, []);
  const agentDefault =
    agents.status === "ready"
      ? (agents.items.find((agent) => agent.id === agentId)?.model ?? null)
      : null;
  // Only the chosen agent's models: the backend refuses any other.
  const agentModels = useMemo(
    () => modelsOf(models, listedModels(agents, agentId)),
    [models, agents, agentId],
  );
  const [modelId, chooseModel, forgetChosenModel] = useChosenModel(
    agentModels,
    agentDefault,
  );
  const forgetModel = useCallback(
    (id: string) => {
      forgetChosenModel(id);
      modelsStale();
    },
    [forgetChosenModel, modelsStale],
  );
  // The side panel's drawer, on the right, below the breakpoint. The left
  // panel's drawer is the shell's.
  const [sideDrawer, setSideDrawer] = useState(false);
  const sideOpener = useRef<HTMLButtonElement>(null);
  const sideCloser = useRef<HTMLButtonElement>(null);

  // Closing puts the focus back on the button that opened it, so a keyboard
  // is never left on an element that has slid off the screen -- or, worse,
  // on <body>, which is where the focus falls when what held it goes away.
  const closeSide = useCallback(() => {
    if (sideDrawer) sideOpener.current?.focus();
    setSideDrawer(false);
  }, [sideDrawer]);

  // Escape closes the drawer, wherever the focus is inside it, and it is a
  // close like any other: the same `close`, so the focus comes back.
  useEffect(() => {
    if (!sideDrawer) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      closeSide();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
    };
  }, [sideDrawer, closeSide]);

  // The focus follows the drawer into it when it opens, so a keyboard
  // carries on where the eye is rather than behind the overlay.
  useEffect(() => {
    if (sideDrawer) sideCloser.current?.focus();
  }, [sideDrawer]);

  // The search page is drawn over the chat, which stays mounted under it (and
  // so keeps its draft and any turn streaming): the chat's own route is the
  // last one that was not the search page.
  const [chatRoute, setChatRoute] = useState<Route>(
    route.kind === "search" ? NEW_CHAT : route,
  );
  const searching = route.kind === "search";
  if (route.kind !== "search" && !sameRoute(route, chatRoute)) {
    setChatRoute(route);
  }
  const view = route.kind === "search" ? chatRoute : route;
  const current = view.kind === "conversation" ? view.id : null;
  const listed = conversationIn(history.items, current);
  // What the chat's own read said about the conversation on the screen,
  // which the panel's pages need not hold; the title is the panel's first,
  // since renaming happens there.
  const [opened, setOpened] = useState<Opened | null>(null);
  const about = current !== null && opened?.id === current ? opened : null;
  const title = listed?.title ?? about?.title ?? null;
  const conversationAgent = listed?.agent ?? about?.agent ?? null;
  const withAgent =
    conversationAgent === null ? null : agentTitle(agents, conversationAgent);
  const conversationModels = useMemo(
    () => modelsOf(models, listedModels(agents, conversationAgent)),
    [models, agents, conversationAgent],
  );
  // Held rather than made at every render: the React Compiler takes a
  // value made from the panel's row by a call of ours for one that may be
  // changed later, and then gives up on every callback below.
  const listedAs = useMemo(
    () => (listed === null ? null : openedFrom(listed)),
    [listed],
  );
  const conversationModel =
    freshest(listedAs, about)?.model ?? listed?.model ?? null;
  const conversationOpened = useCallback((conversation: Conversation) => {
    setOpened((was) => later(was, openedFrom(conversation)));
  }, []);
  // Which conversation is on the screen when an answer lands, which is not
  // necessarily the one it was on when the render that asked was made.
  const onScreen = useRef(current);
  useEffect(() => {
    onScreen.current = current;
  }, [current]);
  // A model change answers with the conversation as it left it, and dates
  // it, which moves it in the panel's order: the list is asked for again, as
  // after a rename.
  //
  // **The answer is what the shell holds about that conversation from then
  // on**, whatever it held before: opened from the panel, the page may still
  // hold the conversation it came from while the chat's read of this one is
  // in the air, and that read -- answered before the change was saved --
  // must then meet the change's answer and lose to it (`later`). It is never
  // written over another conversation the page has moved on to.
  const modelMoved = useCallback(
    (moved: Conversation) => {
      setOpened((was) =>
        was?.id === moved.id
          ? later(was, openedFrom(moved))
          : moved.id === onScreen.current
            ? openedFrom(moved)
            : was,
      );
      history.refresh();
    },
    [history],
  );
  const welcome = (
    <div className="shell-welcome">
      <ProjectPicker
        projects={projects}
        chosen={projectId}
        onChoose={chooseProject}
      />
      <AgentPicker agents={agents} chosen={agentId} onChoose={chooseAgent} />
      {/* Only once there is an agent to talk to: with none, the agent
          picker's sentence is the whole of what there is to say. */}
      {agentId !== null && (
        <ModelPicker
          models={agentModels}
          chosen={modelId}
          onChoose={chooseModel}
        />
      )}
    </div>
  );
  const startedConversation = useCallback(
    (id: string) => {
      // The tabs opened on the empty chat are this conversation's now.
      carryOpenTabs(NEW_CHAT_TABS, id);
      navigate({ kind: "conversation", id });
      // A conversation that has just been created is not in the panel's
      // list, and its title is the beginning of the message that created it
      // (`docs/specs/conversations.md`). Its agent and its model are the
      // ones just chosen.
      if (agentId !== null) {
        setOpened({
          id,
          agent: agentId,
          title: null,
          model: modelId,
          updatedAt: null,
        });
      }
      history.refresh();
    },
    [history, agentId, modelId, navigate],
  );

  // The side panel (`../side/`) offers the agent's MCP apps: the
  // conversation's agent, or on the empty chat the one picked for it.
  const sideAgent = current === null ? agentId : conversationAgent;
  const apps = useApps(sideAgent);
  const [results] = useState(toolResults);
  // A turn can change what an open app shows, so its tab reads it again.
  const [turnsEnded, setTurnsEnded] = useState(0);
  const turnEnded = () => {
    history.refresh();
    setTurnsEnded((count) => count + 1);
  };
  const renderers: TabRenderers = {
    app: (offer) => {
      const app = appOf(apps, offer);
      if (sideAgent === null || app === undefined) return null;
      // Keyed by the agent: the same app of another agent is another view.
      return (
        <AppTab
          key={sideAgent}
          agent={sideAgent}
          app={app}
          turnsEnded={turnsEnded}
          results={results}
        />
      );
    },
  };
  const offers = useMemo(() => appOffers(apps), [apps]);

  return (
    <div className="shell">
      <ShellPanel>
        <Panel
          history={history}
          current={current}
          projects={projects.status === "ready" ? projects.items : null}
          folded={folded}
          onNewChatIn={(project) => {
            chooseProject(project);
            navigate(NEW_CHAT);
            setChat((count) => count + 1);
          }}
          onNewProject={newProject}
          collapsed={collapsed}
          onCollapse={setCollapsed}
          onNewChat={() => {
            navigate(NEW_CHAT);
            setChat((count) => count + 1);
          }}
          onSearch={() => {
            navigate({ kind: "search" });
          }}
          searching={searching}
        />
      </ShellPanel>
      {sideDrawer && (
        <div
          data-backdrop=""
          aria-hidden="true"
          onClick={closeSide}
          className="shell-backdrop"
        />
      )}
      <div className="shell-column">
        {/* The one thing above the chat, and only where the side panel is a
            drawer: `docs/specs/frontend.md` says there is no top bar, so
            above the breakpoint this is gone and nothing replaces it. The
            left panel's drawer opens from the shell's own button. */}
        <div className="shell-openers">
          <button
            type="button"
            ref={sideOpener}
            title="Open the side panel"
            aria-label="Open the side panel"
            aria-controls={SIDE_ID}
            aria-expanded={sideDrawer}
            onClick={() => {
              setSideDrawer(true);
            }}
            className="shell-opener"
          >
            <PanelRightIcon className="shell-icon" />
          </button>
        </div>
        {/* The heading is the first line of the page, because
            `docs/specs/frontend.md` leaves no top bar to put it in. It is the
            shell's and not the chat's: the title comes from the panel's list,
            which is where renaming happens.

            **The chat is not keyed by the conversation.** A first message
            creates the conversation and the route follows it, and remounting
            on that would throw away the stream that is arriving. Which
            conversation is on the screen is a prop it handles itself; the
            `chat` count is what "New chat" remounts it by, so that nothing
            typed into one carries over into the next. */}
        <main className="shell-main">
          {searching && (
            <SearchPage
              projects={projects.status === "ready" ? projects.items : null}
              agentName={(id) => agentTitle(agents, id) ?? id}
            />
          )}
          <div hidden={searching} className="shell-chat">
            <h1 className="shell-title">
              {route.kind === "conversation"
                ? title === null
                  ? "…"
                  : shownTitle(title)
                : "New chat"}
            </h1>
            {/* Who the conversation is with, under its title. The agent
              picker is gone once a conversation exists (`./AgentPicker.tsx`),
              and with it the only thing on the screen that said so; this is
              the fact it left behind, told as a line rather than a control
              that could not be changed. The model is still a choice, so
              beside it is the model picker (`./ModelPicker.tsx`). */}
            {withAgent !== null && (
              <div className="shell-about">
                <p data-agent="" className="shell-agent">
                  with {withAgent}
                </p>
                {current !== null && conversationModel !== null && (
                  <ConversationModel
                    key={current}
                    models={conversationModels}
                    conversationId={current}
                    model={conversationModel}
                    onMoved={modelMoved}
                    onNotOffered={modelsStale}
                  />
                )}
              </div>
            )}
            <Chat
              key={chat}
              conversationId={current}
              agentId={agentId}
              projectId={projectId}
              // The model goes with every turn: an open conversation's is the
              // one its line's picker shows.
              modelId={current === null ? modelId : conversationModel}
              // A first message refused for its model: this browser forgets
              // it, whether or not the list has come, and asks for the list
              // again, so the next one goes to a model still offered.
              onModelRefused={forgetModel}
              onConversationStarted={startedConversation}
              onConversationOpened={conversationOpened}
              onTurnEnded={turnEnded}
              changes={changes}
              onToolResult={results.publish}
              welcome={welcome}
              drafts={drafts}
            />
          </div>
        </main>
      </div>
      <SidePanel
        conversation={current ?? NEW_CHAT_TABS}
        offers={offers}
        renderers={renderers}
        drawer={sideDrawer}
        onCloseDrawer={closeSide}
        closeRef={sideCloser}
      />
    </div>
  );
}
