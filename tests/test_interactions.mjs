/** Run the actual behavior-pack handlers against deterministic Script API doubles.
 * These tests verify bridge/control/inventory behavior, not native Bedrock physics.
 * Usage: node --test tests/test_interactions.mjs
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const main = ["endstone_arc_fake_player", "arc_fake_player", "endstone_arc_fake_player"]
    .map((pkg) => path.join(root, pkg, "behavior_pack/scripts/main.js"))
    .find((name) => fs.existsSync(name));
const source = fs.readFileSync(main, "utf8").replace(/^import .*$/gm, "");

class Stack {
    constructor(typeId, amount = 1, metadata = {}) {
        if (!typeId || typeId === "minecraft:planks") throw new Error("invalid modern item ID");
        this.typeId = typeId;
        this.amount = amount;
        this.metadata = structuredClone(metadata);
        this.maxAmount = typeId.endsWith("_sword") || typeId.endsWith("_pickaxe") ? 1 : 64;
    }
    clone() { return new Stack(this.typeId, this.amount, this.metadata); }
    isStackableWith(other) {
        return this.maxAmount > 1 && this.typeId === other.typeId &&
            JSON.stringify(this.metadata) === JSON.stringify(other.metadata);
    }
}

class Container {
    constructor(size = 36) { this.size = size; this.slots = Array(size).fill(undefined); }
    getItem(slot) {
        assert.ok(Number.isInteger(slot) && slot >= 0 && slot < this.size);
        return this.slots[slot]?.clone();
    }
    setItem(slot, item) {
        assert.ok(Number.isInteger(slot) && slot >= 0 && slot < this.size);
        if (this.failNext) { this.failNext = false; throw new Error("write rejected"); }
        this.slots[slot] = item?.clone();
    }
    swapItems(slot, otherSlot, other) {
        const current = this.getItem(slot);
        this.setItem(slot, other.getItem(otherSlot));
        other.setItem(otherSlot, current);
    }
    count(typeId) { return this.slots.reduce((sum, it) => sum + (it?.typeId === typeId ? it.amount : 0), 0); }
}

function fixture() {
    const replies = [], commands = [], intervals = new Map(), blocks = new Map();
    const inventory = new Container();
    let clock = 0, nextTimer = 1, nextRequest = 1, eventHandler;
    const key = (loc) => [loc.x, loc.y, loc.z].join(",");
    const dimension = {
        id: "minecraft:overworld",
        getBlock(loc) {
            if (blocks.has(key(loc))) return blocks.get(key(loc));
            return { typeId: "minecraft:air", isAir: true, isLiquid: false, location: { ...loc } };
        },
        getEntities() { return []; },
        runCommand(command) {
            commands.push(command);
            const match = command.match(/^scriptevent (\S+) (.+)$/);
            if (match) replies.push({ event: match[1], ...JSON.parse(match[2]) });
        },
    };
    const sim = {
        id: "bot-entity", isValid: true, location: { x: 0.5, y: 64, z: 0.5 },
        dimension, selectedSlotIndex: 0, breaks: 0, stops: 0,
        getComponent(name) { return name === "minecraft:inventory" ? { container: inventory } : undefined; },
        lookAtBlock() {}, stopMoving() {}, stopInteracting() {}, stopUsingItem() {},
        stopBreakingBlock() { this.stops++; },
        breakBlock() { this.breaks++; return true; },
        disconnect() { this.isValid = false; },
        jump() { return true; },
        attackEntity(target) { this.attacked = target; return true; },
        useItemInSlot(slot) { this.usedSlot = slot; return true; },
        useItemInSlotOnBlock(slot, loc, direction) {
            this.placed = { slot, loc: { ...loc }, direction };
            const delta = { Up: [0, 1, 0], Down: [0, -1, 0], East: [1, 0, 0],
                West: [-1, 0, 0], South: [0, 0, 1], North: [0, 0, -1] }[direction];
            const target = { x: loc.x + delta[0], y: loc.y + delta[1], z: loc.z + delta[2] };
            const item = inventory.getItem(slot);
            putBlock(target, item.typeId);
            if (item.amount === 1) inventory.setItem(slot);
            else { item.amount--; inventory.setItem(slot, item); }
            return true;
        },
    };
    function putBlock(loc, typeId = "minecraft:stone", container) {
        const block = { typeId, location: { ...loc }, isAir: false, isLiquid: false,
            getComponent() { return container ? { container } : undefined; } };
        blocks.set(key(loc), block);
        return block;
    }
    const chain = { structureName() { return this; }, maxTicks() { return this; }, tag() { return this; } };
    const sandbox = {
        console: { log() {}, warn() {} }, ItemStack: Stack,
        EquipmentSlot: { Head: "Head", Chest: "Chest", Legs: "Legs",
            Feet: "Feet", Offhand: "Offhand", Mainhand: "Mainhand" },
        world: { getDimension() { return dimension; }, getDefaultGameMode() { return "Adventure"; } },
        gametest: { register() { return chain; }, Tags: { suiteDefault: "default" }, spawnSimulatedPlayer() { return sim; } },
        system: {
            afterEvents: { scriptEventReceive: { subscribe(callback) { eventHandler = callback; } } },
            run() {},
            runInterval(callback, delay = 1) { const id = nextTimer++; intervals.set(id, { callback, delay }); return id; },
            clearRun(id) { intervals.delete(id); },
        },
    };
    vm.runInNewContext(source + "\nthis.api = { sims, resolveGameMode };", sandbox, { filename: main });
    sandbox.api.sims.set("Alex", sim);
    return {
        sim, inventory, dimension, replies, commands, putBlock, blocks, api: sandbox.api,
        tick(amount = 1) {
            for (let i = 0; i < amount; i++) {
                clock++;
                for (const [id, timer] of [...intervals]) {
                    if (intervals.has(id) && clock % timer.delay === 0) timer.callback();
                }
            }
        },
        send(action, payload = {}) {
            const id = nextRequest++;
            eventHandler({ id: "bot:" + action, message: JSON.stringify({ n: "Alex", t: "test", i: id, ...payload }) });
            return id;
        },
        responses(id) { return replies.filter((reply) => reply.i === id && ["bot:result", "bot:dug"].includes(reply.event)); },
        response(id) { const all = this.responses(id); assert.equal(all.length, 1); return all[0]; },
    };
}

const target = { x: 1, y: 64, z: 0 };

test("dig keeps issuing native break calls until the world actually changes", () => {
    const f = fixture();
    f.putBlock(target);
    f.sim.breakBlock = function () { if (++this.breaks === 8) f.blocks.delete("1,64,0"); return true; };
    const id = f.send("dig", target);
    f.tick(7);
    assert.equal(f.responses(id).length, 0);
    f.tick(4);
    assert.equal(f.response(id).ok, true);
    assert.equal(f.sim.breaks, 8);
});

test("duplicate dig joins completion while a new target cancels the previous target once", () => {
    const f = fixture();
    f.putBlock(target);
    const other = { x: 2, y: 64, z: 0 };
    f.putBlock(other);
    const first = f.send("dig", target), duplicate = f.send("dig", target);
    f.tick(2);
    const replacement = f.send("dig", other);
    assert.equal(f.response(first).ok, false);
    assert.equal(f.response(duplicate).ok, false);
    f.blocks.delete("2,64,0");
    f.tick();
    assert.equal(f.response(replacement).ok, true);
});

test("an unloaded block never reports successful digging", () => {
    const f = fixture();
    f.blocks.set("1,64,0", undefined);
    f.sim.breakBlock = () => false;
    const id = f.send("dig", target);
    f.tick();
    assert.equal(f.response(id).ok, false);
});

test("changing dimension ends digging before any further native hit", () => {
    const f = fixture();
    f.putBlock(target);
    const id = f.send("dig", target);
    f.sim.dimension = { id: "minecraft:nether" };
    f.tick();
    assert.equal(f.response(id).ok, false);
    assert.equal(f.sim.breaks, 0);
});

test("placement uses owned inventory and a supporting block without overwriting the hotbar", () => {
    const f = fixture();
    f.inventory.setItem(0, new Stack("minecraft:diamond", 3));
    f.inventory.setItem(12, new Stack("minecraft:cobblestone", 4));
    f.putBlock({ x: 1, y: 63, z: 0 });
    const id = f.send("place", { ...target, block: "cobblestone" });
    assert.equal(f.responses(id).length, 0);
    f.tick();
    assert.equal(f.response(id).ok, true);
    assert.deepEqual(f.sim.placed.loc, { x: 1, y: 63, z: 0 });
    assert.equal(f.sim.placed.direction, "Up");
    assert.equal(f.inventory.count("minecraft:diamond"), 3);
    assert.equal(f.inventory.count("minecraft:cobblestone"), 3);
    assert.equal(f.commands.some((command) => command.startsWith("setblock")), false);
});

test("placement rejects missing materials and engine interactions that do not place a block", () => {
    const f = fixture();
    f.putBlock({ x: 1, y: 63, z: 0 });
    assert.equal(f.response(f.send("place", { ...target, block: "stone" })).ok, false);
    f.inventory.setItem(0, new Stack("minecraft:stone"));
    f.sim.useItemInSlotOnBlock = () => true;
    const id = f.send("place", { ...target, block: "stone" });
    f.tick(6);
    assert.equal(f.response(id).ok, false);
    assert.equal(f.inventory.count("minecraft:stone"), 1);
});

test("consume uses an actual inventory slot and waits for native consumption", () => {
    const f = fixture();
    f.inventory.setItem(14, new Stack("minecraft:bread", 3));
    const id = f.send("consume", { item: "bread" });
    assert.equal(f.sim.usedSlot, 14);
    f.tick(30);
    assert.equal(f.responses(id).length, 0);
    f.inventory.setItem(14, new Stack("minecraft:bread", 2));
    f.tick();
    assert.equal(f.response(id).ok, true);
});

test("consume times out honestly when hunger is full, and stop settles it once", () => {
    const f = fixture();
    f.inventory.setItem(0, new Stack("minecraft:bread"));
    const full = f.send("consume", { item: "bread" });
    f.tick(101);
    assert.equal(f.response(full).ok, false);
    const canceled = f.send("consume", { item: "bread" });
    f.send("stop");
    f.tick(101);
    assert.equal(f.response(canceled).ok, false);
});

test("select swaps an inventory slot into the hotbar and preserves both stacks", () => {
    const f = fixture();
    f.inventory.setItem(0, new Stack("minecraft:bread", 5));
    f.inventory.setItem(15, new Stack("minecraft:iron_pickaxe", 1, { durability: 17, name: "Mine" }));
    assert.equal(f.response(f.send("select", { item: "iron_pickaxe" })).ok, true);
    assert.equal(f.inventory.getItem(0).metadata.durability, 17);
    assert.equal(f.inventory.getItem(15).typeId, "minecraft:bread");
});

test("put combines source stacks and preserves actual alias IDs and metadata", () => {
    const f = fixture(), chest = new Container(3);
    f.putBlock(target, "minecraft:chest", chest);
    f.inventory.setItem(2, new Stack("minecraft:birch_planks", 2, { name: "A" }));
    f.inventory.setItem(3, new Stack("minecraft:oak_planks", 3));
    assert.equal(f.response(f.send("container", { ...target, op: "put", item: "planks", count: 4 })).ok, true);
    assert.equal(chest.count("minecraft:birch_planks"), 2);
    assert.equal(chest.count("minecraft:oak_planks"), 2);
    assert.equal(chest.getItem(0).metadata.name, "A");
    assert.equal(f.inventory.count("minecraft:oak_planks"), 1);
});

test("full destination does not partially move or lose either inventory", () => {
    const f = fixture(), chest = new Container(1);
    f.putBlock(target, "minecraft:chest", chest);
    f.inventory.setItem(0, new Stack("minecraft:stone", 4));
    chest.setItem(0, new Stack("minecraft:stone", 63));
    assert.equal(f.response(f.send("container", { ...target, op: "put", item: "stone", count: 4 })).ok, false);
    assert.equal(f.inventory.count("minecraft:stone"), 4);
    assert.equal(chest.count("minecraft:stone"), 63);
});

test("a rejected write rolls back the earlier source inventory write", () => {
    const f = fixture(), chest = new Container(1);
    f.putBlock(target, "minecraft:chest", chest);
    f.inventory.setItem(0, new Stack("minecraft:stone", 4));
    chest.failNext = true;
    assert.equal(f.response(f.send("container", { ...target, op: "put", item: "stone", count: 4 })).ok, false);
    assert.equal(f.inventory.count("minecraft:stone"), 4);
    assert.equal(chest.count("minecraft:stone"), 0);
});

test("take refuses a full player inventory without deleting container items", () => {
    const f = fixture(), chest = new Container(1);
    f.putBlock(target, "minecraft:chest", chest);
    for (let slot = 0; slot < f.inventory.size; slot++) f.inventory.setItem(slot, new Stack("minecraft:dirt", 64));
    chest.setItem(0, new Stack("minecraft:diamond", 3, { name: "gift" }));
    assert.equal(f.response(f.send("container", { ...target, op: "take", item: "diamond", count: 2 })).ok, false);
    assert.equal(chest.count("minecraft:diamond"), 3);
    assert.equal(chest.getItem(0).metadata.name, "gift");
});

test("craft checks aggregate material demand before any consumption", () => {
    const f = fixture();
    f.inventory.setItem(0, new Stack("minecraft:oak_planks", 1));
    const id = f.send("container", { op: "craft", grid: 2, result: "crafting_table", count: 1,
        slots: [0, 1, 2, 3].map((slot) => ({ slot, id: "planks" })) });
    assert.equal(f.response(id).ok, false);
    assert.equal(f.inventory.count("minecraft:oak_planks"), 1);
    assert.equal(f.inventory.count("minecraft:crafting_table"), 0);
});

test("plank crafting produces the matching wood species", () => {
    const f = fixture();
    f.inventory.setItem(0, new Stack("minecraft:birch_log", 2));
    const id = f.send("container", { op: "craft", grid: 2, result: "planks", count: 4, slots: [{ slot: 0, id: "log" }] });
    assert.equal(f.response(id).made.id, "minecraft:birch_planks");
    assert.equal(f.inventory.count("minecraft:birch_planks"), 4);
    assert.equal(f.inventory.count("minecraft:birch_log"), 1);
});

test("table crafting works without assuming a nonexistent ten-slot table inventory", () => {
    const f = fixture();
    f.putBlock(target, "minecraft:crafting_table");
    f.inventory.setItem(0, new Stack("minecraft:cobblestone", 8));
    const id = f.send("container", { ...target, op: "craft", grid: 3, result: "furnace", count: 1,
        slots: [0, 1, 2, 3, 5, 6, 7, 8].map((slot) => ({ slot, id: "cobblestone" })) });
    assert.equal(f.response(id).ok, true);
    assert.equal(f.inventory.count("minecraft:furnace"), 1);
    assert.equal(f.inventory.count("minecraft:cobblestone"), 0);
});

test("furnace waits beyond 120 ticks and only finishes after the requested count", () => {
    const f = fixture(), furnace = new Container(3);
    f.putBlock(target, "minecraft:furnace", furnace);
    f.inventory.setItem(0, new Stack("minecraft:raw_iron", 2));
    f.inventory.setItem(1, new Stack("minecraft:charcoal", 4));
    const id = f.send("container", { ...target, op: "smelt", input: "raw_iron", fuel: "coal", count: 2 });
    f.tick(201);
    assert.equal(f.responses(id).length, 0);
    furnace.setItem(2, new Stack("minecraft:iron_ingot"));
    f.tick();
    assert.equal(f.responses(id).length, 0);
    f.tick(200);
    furnace.setItem(2, new Stack("minecraft:iron_ingot"));
    f.tick();
    assert.equal(f.response(id).made.count, 2);
    assert.equal(f.inventory.count("minecraft:iron_ingot"), 2);
    assert.equal(f.inventory.count("minecraft:charcoal"), 3);
});

test("smelting refuses occupied furnaces without stealing or overwriting contents", () => {
    const f = fixture(), furnace = new Container(3);
    f.putBlock(target, "minecraft:furnace", furnace);
    furnace.setItem(2, new Stack("minecraft:gold_ingot", 2));
    assert.equal(f.response(f.send("container", { ...target, op: "smelt", input: "raw_iron", count: 1 })).ok, false);
    assert.equal(furnace.count("minecraft:gold_ingot"), 2);
});

test("attack supports named players, and jump preserves the native refusal result", () => {
    const f = fixture();
    const player = { id: "player", typeId: "minecraft:player", name: "Steve", location: { ...target } };
    f.dimension.getEntities = () => [f.sim, player];
    assert.equal(f.response(f.send("attack", { target: "Steve" })).ok, true);
    assert.equal(f.sim.attacked, player);
    f.sim.jump = () => false;
    assert.equal(f.response(f.send("jump")).ok, false);
});

test("spawn mode converts wire integers to the Script API string enum", () => {
    const f = fixture();
    assert.equal(f.api.resolveGameMode(0), "Survival");
    assert.equal(f.api.resolveGameMode(1), "Creative");
    assert.equal(f.api.resolveGameMode("survival"), "Survival");
    assert.equal(f.api.resolveGameMode(undefined), "Adventure");
    assert.equal(f.api.resolveGameMode(1.5), "Adventure");
});
