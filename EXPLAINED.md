# The project, explained from scratch

No prior Kubernetes knowledge assumed. We build one idea at a time, and each new
piece only uses ideas already introduced.

One analogy runs through the whole document: **an office building**. It is used
consistently, so once you learn what a "floor" means here it keeps meaning that.
Every section ends with **In real terms** giving you the actual vocabulary, so
you finish with the real words and not just the story.

---

## 1. What is this project, in one paragraph?

We built a small, complete, working data centre on a laptop — and then we proved
it works.

Not "it started without errors". Proved: we sent traffic between machines and
watched it arrive. We wrote a file, destroyed the computer holding it, and
checked the file came back. We measured how full a disk was and confirmed the
number was true. There are 53 automated checks, and every one of them talks to
the real running system.

The reason to build it broken-first and prove each layer is that you learn what
each piece actually *does* by watching what fails without it.

---

## 2. The building

Imagine an **office building** where programs live and work instead of people.

- The **building** is the cluster — the whole system.
- Each **floor** is a *node*: one computer.
- Each **room** on a floor is a *pod*: one running program.
- The **building manager** decides which room each new program gets.

Our building has three floors:

```
┌─────────────────────────────────────────┐
│  Floor 1  "control-plane"  the office   │  ← the manager works here
│  Floor 2  "worker"         workspace    │  ← programs live here
│  Floor 3  "worker2"        workspace    │  ← and here
└─────────────────────────────────────────┘
```

Floor 1 is management. It keeps the list of who lives where and decides where new
programs go. Floors 2 and 3 are where the actual work happens.

**Why three floors and not one?** Because we want to test whether programs on
*different* floors can talk to each other. If everything lived on one floor,
they could shout across the room and we would learn nothing about the building's
phone system. Two workers force real communication between floors.

> **In real terms:** the cluster is Kubernetes. Nodes are machines. Pods are the
> smallest runnable unit — usually one container. The "manager" is the control
> plane: the API server, scheduler and controller manager.

---

## 3. Where the building actually is

Here is the strange part. Your laptop is a Mac, and this kind of building can
only be constructed in Linux. So we build a Linux world *inside* the Mac, and
put the building inside that:

```
Your Mac
└── a Linux virtual machine        ← "Colima". A pretend computer, running Linux
    └── Docker                     ← runs things in isolated boxes
        └── 3 boxes                ← each box pretends to be one "floor"
            └── Kubernetes         ← the building
```

Four layers deep. This matters for one practical reason: **when something
breaks, you have to work out which layer broke.** A program that cannot reach
the internet might be failing at the building's phone system, or Docker's, or
the virtual machine's, or your actual Wi-Fi — and from inside the room, all four
look identical.

There is one consequence of this stacking that trips up almost everyone, and we
come back to it in §10.

> **In real terms:** Colima runs a Linux VM using Apple's Virtualization
> framework. `kind` ("Kubernetes in Docker") makes each node a Docker container.
> Our VM has 4 CPUs and 7.74 GiB of memory.

---

## 4. We built the building with no phone system — on purpose

When a new program moves into a room, it needs to talk to programs in other
rooms. That requires a phone system: wiring, an address for each room, and a
switchboard that routes calls between floors.

In Kubernetes this is the **CNI** — Container Network Interface. It is not
included by default; you choose and install one.

**We deliberately built the building with the phone system left out.** Here is
what that looked like:

```
NAME                        STATUS
k8s-cni-lab-control-plane   NotReady
k8s-cni-lab-worker          NotReady
k8s-cni-lab-worker2         NotReady
```

`NotReady` on every floor. The building says, in effect: *I exist, but nobody can
move in yet.* The precise complaint was:

```
cni plugin not initialized
```

Now the interesting bit. Some things were running perfectly fine:

| Program | Status | Why |
|---|---|---|
| The manager's software | ✅ Running | Uses the building's own wiring, not the room phone system |
| The record-keeper (etcd) | ✅ Running | Same |
| **CoreDNS** (the phone book) | ❌ Pending | It is an ordinary program in an ordinary room |

That table *is* the lesson. It shows you exactly which parts of Kubernetes depend
on the pod network and which do not. You cannot learn that from a cluster that
just works.

Then we installed the phone system — **Calico** — and within about a minute:

```
NAME                        STATUS
k8s-cni-lab-control-plane   Ready
k8s-cni-lab-worker          Ready
k8s-cni-lab-worker2         Ready
```

CoreDNS, stuck waiting this whole time, moved in by itself.

> **In real terms:** `disableDefaultCNI: true` in the kind config. Programs that
> kept working use `hostNetwork: true` — the node's own network stack.

### Room numbers

Every room gets a number so calls can be routed. We told the building to use
numbers starting `10.244.`, and gave each floor its own block:

| Floor | Room numbers |
|---|---|
| control-plane | `10.244.40.128` – `10.244.40.191` |
| worker | `10.244.247.192` – `10.244.247.255` |
| worker2 | `10.244.67.0` – `10.244.67.63` |

**Why `10.244` specifically?** Calico normally suggests numbers starting
`192.168.` — but that is the same range most home Wi-Fi routers hand out. If the
building used room numbers that clashed with houses on your street, some calls
would reach the wrong place. Occasionally. Unpredictably. That is a genuinely
horrible bug to chase, so we avoided it by choosing a range nothing else uses.

### Did the phone system actually work?

Three tests, each harder than the last:

1. **Direct call.** Program on floor 2 calls a program on floor 3 *by room
   number*. It answered. This tests only the wiring.
2. **Call by name.** Same call, but dialling `web.net-test...` instead of a
   number. The phone book had to translate the name first.
3. **The security test.** This one matters most.

For the third, we put up a rule: *nobody may call these rooms*. Then we called
anyway — and the call **failed**. Then we changed the rule to *only these
programs may call* — and the call **succeeded**.

Why go to the trouble? Because Kubernetes will happily accept a security rule on
a building with no locks at all. It says "rule accepted!" and every call goes
through exactly as before. So "the rule applied successfully" proves *nothing*.
The only proof a lock works is watching a door refuse to open.

> **In real terms:** NetworkPolicy. Many CNIs accept the objects and never
> enforce them. Calico enforces them, and we verify by observing traffic stop.

---

## 5. Storage: rooms forget everything

A program's room is wiped clean when the program stops. Anything it wrote is
gone. That is fine for a calculator and disastrous for a database.

So the building has **storage lockers** in the basement of each floor. A program
asks for one, gets it, and whatever it puts inside survives even if the program
itself is destroyed and replaced.

The software that manages lockers here is **OpenEBS**.

### Asking for a locker

A program files a request: *"I would like a locker, about 1 GB please."* That
request is a **PVC** (PersistentVolumeClaim). The actual locker it receives is a
**PV** (PersistentVolume).

Think: PVC = the application form. PV = the locker.

### "My request is stuck and nothing is happening!"

You file the form and check on it:

```
NAME        STATUS
probe-pvc   Pending
```

Pending. Forever. Nothing happens. This is the single most-reported OpenEBS
"bug", and **it is not a bug.**

Our lockers are physical cupboards on a *specific floor*. The locker manager
cannot build your cupboard until it knows which floor you will be living on —
and nobody knows that until the building manager assigns you a room. So the form
waits.

The moment a program actually moves in and claims it, the locker appears
instantly.

> **In real terms:** `volumeBindingMode: WaitForFirstConsumer`. Binding is
> deferred until a pod is scheduled, because a hostpath volume is a directory on
> one specific node.

### The locker cannot follow you

Because the cupboard is physically bolted to floor 2, a program using it can
*only* ever live on floor 2. If it is destroyed and restarted, the building
manager must put it back on floor 2.

Kubernetes enforces this with something called node affinity. Without it, the
program could restart on floor 3, find an empty cupboard there, and conclude all
its data had vanished — silent data loss, no error message.

### The honest limitations

Our lockers are simple, and simple has costs:

- **The "1 GB" is a polite request, not a limit.** Nothing stops a program
  stuffing 50 GB into a locker it asked 1 GB for. It will keep going until the
  entire floor's storage room is full — and then *every* locker on that floor
  fails at once. We alert on this specifically.
- **There is only one copy.** No backup. If the floor is demolished, the data is
  gone.
- **Demolishing the building deletes everything.** `kind delete cluster` removes
  the floors and the lockers bolted to them.

### Who is using lockers?

| Program | Locker size | Floor |
|---|---|---|
| Prometheus | 8 GB | worker2 |
| Grafana | 2 GB | worker2 |
| Alertmanager | 1 GB | worker |
| Our demo program | 1 GB | worker |

Notice the first three: **the monitoring system stores its own data in these
lockers.** That was a deliberate choice. Instead of building a fake program to
test storage, we made the storage carry something that genuinely matters and
writes constantly. If lockers break, the monitoring breaks loudly and
immediately.

---

## 6. Measuring things: what a metric is

A **metric** is a number about the system at a moment in time.

> `node_memory_MemAvailable_bytes = 4823449600`
>
> "There are 4.8 GB of memory free."

Collect that number every 30 seconds and you can draw a line showing memory over
time. That is all a monitoring graph is: the same number, asked repeatedly,
plotted.

### The meter reader and the wall of screens

**Prometheus** is a meter reader. Every 30 seconds it walks the whole building
with a clipboard, visits every program that has a meter on its door, writes down
every number, and files it with a timestamp. It never forgets and never gets
bored.

**Grafana** is the wall of screens in reception. It cannot measure anything
itself. It reads Prometheus' notebook and draws pictures.

That division confuses people constantly, so: **if a graph is empty, Grafana is
almost never the problem.** Either nobody wrote the number down, or you asked for
it by the wrong name.

In our building the meter reader visits **34 meters** on every round.

> **In real terms:** Prometheus *scrapes* HTTP endpoints — usually `/metrics` —
> and stores time series. Grafana queries Prometheus using PromQL. Each meter is
> a "target"; 34 targets, all healthy.

### Some meters were hidden behind locked doors

Four important programs — the building manager, the room-assigner, the
switchboard, the record-keeper — had meters installed *facing inward*, readable
only from inside their own room. The meter reader, walking the corridor, could
not see them.

The fix had to happen **when the building was constructed**. Once the walls are
up, moving those meters means renovating. So we recreated the whole cluster with
the meters turned outward. That is why the setup destroys and rebuilds the
cluster partway through.

> **In real terms:** kube-controller-manager, kube-scheduler, kube-proxy and etcd
> bind metrics to `127.0.0.1` by default. `kubeadmConfigPatches` in the kind
> config rebind them to `0.0.0.0`. This cannot be changed later without editing
> static pod manifests on the node.

---

## 7. The three kinds of measurement

This is the most useful idea in the whole project, and it is easy to miss.

There are three different *heights* from which you can watch a program, and each
is blind to something the others see.

### Height 1 — the building

*"Is the building running out of electricity, water, floor space?"*

Total memory, total CPU, disk space. Tells you the building is in trouble. Does
**not** tell you which program caused it.

### Height 2 — the room, watched from the corridor

*"This room is using 12% of one CPU and 30 MB of memory. It has been restarted
twice."*

You are watching through a window. You can see the room is warm and the lights
are on. Useful — and it has a serious blind spot.

**A program can look perfectly healthy from the corridor while completely
broken.** Sitting at 2% CPU, memory steady, never crashed — and failing every
single thing it tries to do, silently, for an hour. From outside, that looks
exactly like a program with not much work on.

### Height 3 — inside the room

*"I have attempted 412 saves. 3 failed. My slowest save took 4 milliseconds."*

Only the program itself knows this. And only if someone **taught it to say so**.
This is called *instrumenting* an application.

Our demo program is instrumented. It reports:

| What it says | Meaning |
|---|---|
| `ledger_writes_total` | how many saves attempted |
| `ledger_write_errors_total` | how many failed |
| `ledger_write_duration_seconds` | how long they took |
| `ledger_boots_total` | how many times it has restarted |

You cannot get any of that from heights 1 or 2. It has to come from inside.

**We built one dashboard for each height** — that is why there are three, not
because three looked nicer.

| Screen | Height | Question it answers |
|---|---|---|
| `vm-infra` | Building | Are we running out of anything? |
| `openebs-storage` | Room, from outside | How full is each locker? |
| `app-ledger` | Inside the room | Is the program actually working? |

---

## 8. The measurement that was lying

Here is a real problem we found, and it is a good lesson in not trusting a number
just because it appeared.

We asked the standard, obvious question: *how full is each locker?* The answer:

| Locker | Asked for | Reported size |
|---|---|---|
| demo program | 1 GB | **58.76 GB** |
| Grafana | 2 GB | **58.76 GB** |
| Prometheus | 8 GB | **58.76 GB** |

Three different lockers. Identical answers. And 58.76 GB happens to be the size
of the **entire storage room**.

Why? Our lockers are not really separate boxes — they are *marked-off areas on
the floor of one big storage room*. When you ask "how big is this locker?", the
system measures the room, because there is no wall to measure.

A graph built on this shows three identical flat lines that describe none of the
three lockers. It looks like a working dashboard. It is worse than no dashboard,
because you would trust it.

**The fix:** we wrote a small program that walks into the storage room and
measures each marked-off area directly — the way you would with a tape measure.
Now:

| Locker | Actually used |
|---|---|
| Prometheus | 11.36 MB |
| Grafana | 49.58 MB |
| Alertmanager | 0.00 MB |
| demo program | 34.52 MB |

Different numbers. Real information.

> **In real terms:** `kubelet_volume_stats_*` calls `statfs()` on the mount path,
> which returns the underlying filesystem's figures for hostpath volumes. We ship
> a DaemonSet that measures each PV directory and joins it to kube-state-metrics
> for identity. Full derivation in [docs/07](docs/07-metrics-and-dashboards.md).

---

## 9. The other measurement that was lying — ours

We introduced a bug, and it is worth showing because the *shape* of it is
extremely common.

We wanted to know how long saves take. The usual method is a **histogram** — a
set of tally counters:

```
saves faster than 1 ms:     ▌▌▌▌▌
saves faster than 5 ms:     ▌▌▌▌▌▌▌
saves faster than 10 ms:    ▌▌▌▌▌▌▌▌
```

The rule is that each line counts **everything at or below** it. So each line
must be ≥ the line above.

Our program counted a save into its own bucket, and *then* the reporting step
added them up again. Everything was counted twice. The tallies came out like
this:

```
faster than 1.0 s:   288
faster than 2.5 s:   318
grand total:          30      ← smaller than the line above it!
```

The grand total being *smaller* than one of its parts is impossible. And the
consequence:

> **Reported: "99% of saves finish within 2.3 seconds."**
> **Reality: the average save took 1.3 milliseconds.**

Off by a factor of roughly 1,800.

The thing to notice: **nothing raised an error.** No crash, no warning, no red
text. The graph drew a confident, smooth, completely wrong line. Broken
measurements do not announce themselves — they just quietly lie.

After the fix, the reported figure was **3.7 ms**, which matches reality.

There is a sting in the tail worth knowing. After fixing it, the graph kept
showing the old wrong number for another five minutes, because the calculation
looks at *the last five minutes of readings* and those still contained bad data.
We nearly concluded the fix had failed. It had not — we just had to wait.

We also strengthened the automatic check. The old one verified the grand total
matched — which it did, while everything else was broken. The new one verifies
every line is at least as big as the one above it.

---

## 10. Three windows into the same room

Remember the four-layer stacking from §3? Here is the consequence.

Our three "floors" are not three computers. They are three boxes inside **one**
Linux machine. They share its brain and its memory.

So when we ask each floor "how much memory do you have?", all three answer with
the same number — because there is only one pool of memory:

```
floor 1:  7.74 GB   started at 1785204879
floor 2:  7.74 GB   started at 1785204879
floor 3:  7.74 GB   started at 1785204879
```

Look at the start times. Identical to the second. Three computers do not boot at
the identical instant — that is proof they are one computer wearing three name
badges.

**The trap:** the natural thing to do with three numbers is add them up.

```
7.74 + 7.74 + 7.74 = 23.2 GB   ← we do NOT have 23 GB
```

That is looking through three windows into one room and reporting three rooms.

Several dashboards that ship with the monitoring system do exactly this, because
they assume — correctly, on real clusters — that each node is a separate
machine. Here that assumption is false. **So when you open "Node Exporter /
Nodes", treat its totals with suspicion.** Our `vm-infra` dashboard takes the
*maximum* instead of the sum, and reports 7.74 GB.

Some things genuinely *are* per-floor and are safe to add: how many programs live
on each floor, and network traffic, since each box does have its own network
connection.

---

## 11. The configuration files, in plain language

Every file exists to answer one question.

| File | The question it answers |
|---|---|
| `cluster/kind-config.yaml` | How many floors, what room numbers, no phone system, and turn those hidden meters outward |
| `cluster/calico-installation.yaml` | Install the phone system; use room numbers starting `10.244` |
| `manifests/storage/openebs-values.yaml` | Turn on the simple locker type; turn off the four that need equipment we do not have |
| `manifests/monitoring/kube-prometheus-values.yaml` | Hire the meter reader and put up the screens; both store data in lockers |
| `manifests/monitoring/openebs-hostpath-exporter.yaml` | The tape-measure program from §8 |
| `manifests/monitoring/openebs-monitoring.yaml` | Sums and alerts: "warn me when a locker overflows" |
| `manifests/workload/ledger-statefulset.yaml` | The demo program: writes to a locker, reports on itself |
| `manifests/monitoring/dashboards/` | The three screens |
| `scripts/*.sh` | Do all of the above, in order |
| `validation/*.py` | The 53 checks that prove it worked |

### Two settings that fail silently if wrong

Worth calling out, because neither produces an error message.

**`serviceMonitorSelectorNilUsesHelmValues: false`** — by default the meter
reader only visits meters carrying an official company sticker. Any meter you
install yourself is walked straight past. You would see your meter listed, look
correct, and never get a reading.

**`resourcePath: /metrics/resource`** — the monitoring system still looks for one
particular meter at an address that was removed from Kubernetes years ago. It
knocks on a door that no longer exists and gets "404 Not Found" forever. It hides
well, because the other three meters on that door work fine.

---

## 12. What "proving it works" means here

53 automated checks. None of them use fake data — every one talks to the real
running system. A few examples of how they are built, because the *design* is
the interesting part:

**The storage check destroys something on purpose.** It writes a file with a
unique random name, then deletes the program that wrote it, waits for the
replacement, and looks for the file. It also records the program's *ID number*
before deleting, and refuses to accept an answer until it sees a *different* ID —
otherwise it might accidentally read the original program's files while it was
still shutting down, and prove nothing.

**The network check confirms its own setup first.** It verifies the two test
programs actually landed on different floors before trusting any result. If they
had both landed on floor 2, every "cross-floor" result would be a same-floor
result, and the test would pass while proving nothing.

**The dashboard check runs every single graph.** For all three screens it takes
each graph's question, asks it, and fails if any comes back empty. This exists
because a graph asking for a number that does not exist draws an *empty chart*,
not an error — so nothing else would ever notice.

**The waiting is deliberate.** One check deletes a program, so for a few seconds
afterwards a meter genuinely is unreachable. The checks wait for things to settle
instead of taking one snapshot, because otherwise they would report a failure
that is not real.

---

## 13. Go and look at it yourself

Open **http://localhost:30030** — username `admin`, password `admin`.

Grafana's front page is a greeting, not a list. Click **Dashboards** in the left
menu to find all 32.

Three worth opening, in this order:

**http://localhost:30030/d/vm-infra** — the building. How much CPU and memory the
whole thing is using. This is the max-not-sum dashboard from §10.

**http://localhost:30030/d/openebs-storage** — the lockers. Real per-locker
usage, from §8. Watch the demo program's locker climb.

**http://localhost:30030/d/app-ledger** — inside the room. Saves per second, how
long they take, how many failed. None of this exists without instrumenting the
program.

Then try this experiment. Open the last dashboard, note the "Boots on this
volume" number, and run:

```bash
kubectl -n storage-demo delete pod ledger-0
```

You just destroyed the program. Wait a minute and watch the number go **up by
one** — not reset to zero. The locker survived. The new program opened it and
found the previous program's notes waiting.

That is the whole project in one number.

---

## Where to go next

| If you want | Read |
|---|---|
| The design, with real diagrams | [ARCHITECTURE.md](ARCHITECTURE.md) |
| To build it yourself, by hand | [MANUAL-SETUP.md](MANUAL-SETUP.md) |
| Why the storage metrics were wrong | [docs/07](docs/07-metrics-and-dashboards.md) |
| Something is broken | [docs/08](docs/08-troubleshooting.md) |
| The commands | [README.md](README.md) |
