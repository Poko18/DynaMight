
import torch
import numpy as np
from tsnecuda import TSNE
from sklearn.decomposition import PCA
import umap
import torch.nn.functional as F
from tqdm import tqdm
import matplotlib.pyplot as plt
from matplotlib.backends.backend_qt5agg import FigureCanvas
from matplotlib.figure import Figure
import napari
from qtpy.QtCore import Qt
from magicgui import widgets
from matplotlib.widgets import LassoSelector, PolygonSelector
from ..utils.utils_new import bezier_curve, make_equidistant, compute_threshold
import starfile
from pathlib import Path


def compute_dimensionality_reduction(
        latent_space: torch.Tensor, method: str
) -> np.array:
    if method == 'TSNE':
        embedded_latent_space = TSNE(perplexity=1000.0, num_neighbors=1000,
                                     device=0).fit_transform(latent_space.cpu())
    elif method == 'UMAP':
        embedded_latent_space = umap.UMAP(random_state=12, n_neighbors=100,
                                          min_dist=1.0).fit_transform(latent_space.cpu().numpy())
    elif method == 'PCA':
        embedded_latent_space = PCA(n_components=2).fit_transform(
            latent_space.cpu().numpy())
    return embedded_latent_space


def compute_latent_space_and_colors(
        encoder: torch.nn.Module,
        decoder: torch.nn.Module,
        dataloader: torch.utils.data.DataLoader,
        poses: torch.nn.Module,
        data_preprocessor,
        indices
):
    device = decoder.device

    color_euler_angles = poses.orientations[indices]
    color_euler_angles = F.normalize(color_euler_angles, dim=1)
    color_euler_angles = color_euler_angles.detach().cpu().numpy()
    color_euler_angles = color_euler_angles / 2 + 0.5
    color_euler_angles = color_euler_angles[:, 2]

    color_shifts = poses.translations[indices]
    color_shifts = torch.linalg.norm(color_shifts, dim=1)
    color_shifts = color_shifts.detach().cpu().numpy()
    color_shifts = color_shifts - np.min(color_shifts)
    color_shifts /= np.max(color_shifts)

    '-----------------------------------------------------------------------------'
    'Evaluate model on the half-set'

    global_distances = torch.zeros(decoder.model_positions.shape[0]).to(device)

    with torch.no_grad():
        for batch_ndx, sample in enumerate(tqdm(dataloader)):
            r, y, ctf = sample["rotation"], sample["image"], sample["ctf"]
            idx = sample['idx']
            r, t = poses(idx)
            y = data_preprocessor.apply_square_mask(y)
            y = data_preprocessor.apply_translation(y, -t[:, 0], -t[:, 1])
            y = data_preprocessor.apply_circular_mask(y)
            ctf = torch.fft.fftshift(ctf, dim=[-1, -2])
            y, r, ctf, t = y.to(device), r.to(
                device), ctf.to(device), t.to(device)
            mu, _ = encoder(y, ctf)
            _, _, displacements = decoder(mu, r, t)
            displacement_norm = torch.linalg.vector_norm(displacements, dim=-1)
            mean_displacement_norm = torch.mean(displacement_norm, 0)
            global_distances += mean_displacement_norm
            model_positions = torch.stack(
                displacement_norm.shape[0] * [decoder.model_positions], 0)

            if batch_ndx == 0:
                z = mu
                color_amount = torch.sum(displacement_norm, 1)
                color_direction = torch.mean(displacements.movedim(2, 0)
                                             * displacement_norm, -1)
                color_position = torch.mean(
                    model_positions.movedim(2, 0) * displacement_norm, -1)

            else:
                z = torch.cat([z, mu])
                color_amount = torch.cat(
                    [color_amount, torch.sum(displacement_norm, 1)])
                color_direction = torch.cat(
                    [color_direction, torch.mean(displacements.movedim(2, 0) * displacement_norm, -1)], 1)
                color_position = torch.cat([color_position,
                                            torch.mean(model_positions.movedim(
                                                2, 0) * displacement_norm, -1)], 1)

    color_direction = torch.movedim(color_direction, 0, 1)
    color_direction = F.normalize(color_direction, dim=1)
    color_direction = (color_direction + 1)/2
    color_position = torch.movedim(color_position, 0, 1)
    max_position = torch.max(torch.abs(color_position))
    color_position /= max_position
    color_position = (color_position+1)/4+0.5

    mean_deformation = global_distances / torch.max(global_distances)
    mean_deformation = mean_deformation.cpu().numpy()

    color_amount = color_amount.cpu().numpy()
    color_amount = color_amount / np.max(color_amount)
    color_direction = color_direction.cpu().numpy()
    color_position = color_position.cpu().numpy()

    consensus_positions = decoder.model_positions.detach().cpu().numpy()
    consensus_color = consensus_positions/max_position.cpu().numpy()
    consensus_color = np.mean((consensus_color+1)/4+0.5, -1)

    amps = decoder.amp.detach().cpu()
    amps = amps[0]
    amps -= torch.min(amps)
    amps /= torch.max(amps)
    widths = torch.nn.functional.softmax(decoder.ampvar, 0)[0].detach().cpu()

    lat_colors = {'amount': color_amount, 'direction': color_direction, 'location': color_position, 'index': indices, 'pose': color_euler_angles,
                  'shift': color_shifts}
    point_colors = {'activity': mean_deformation, 'amplitude': amps,
                    'width': widths, 'position': consensus_color}

    return z, lat_colors, point_colors


class Visualizer:
    def __init__(self, z, latent_space, decoder, V0, cons_volume, nap_cons_pos, point_colors,
                 latent_colors, starfile, indices, latent_closest, half_set, output_directory, atomic_model=None):
        self.z = z
        self.latent_space = latent_space
        self.vol_viewer = napari.Viewer(ndisplay=3)
        self.star = starfile
        self.decoder = decoder
        self.device = decoder.device
        self.cons_volume = cons_volume / np.max(cons_volume)
        self.V0 = (V0[0] / torch.max(V0)).cpu().numpy()
        self.threshold = compute_threshold(torch.tensor(self.V0))
        self.vol_layer = self.vol_viewer.add_image(self.V0, rendering='iso', colormap='gray',
                                                   iso_threshold=self.threshold, name='flexible map')
        self.cons_layer = self.vol_viewer.add_image(self.cons_volume, rendering='iso',
                                                    colormap='gray', iso_threshold=self.threshold, name='consensus map', visible=False)
        self.point_properties = point_colors
        self.nap_cons_pos = nap_cons_pos
        self.nap_zeros = torch.zeros(nap_cons_pos.shape[0])
        self.point_layer = self.vol_viewer.add_points(self.nap_cons_pos,
                                                      properties=self.point_properties, ndim=4,
                                                      size=1.5, face_color='activity',
                                                      face_colormap='jet', edge_width=0,
                                                      visible=False, name='consensus positions')
        self.cons_point_layer = self.vol_viewer.add_points(self.nap_cons_pos,
                                                           properties=self.point_properties, ndim=4,
                                                           size=1.5, face_color='activity',
                                                           face_colormap='jet', edge_width=0,
                                                           visible=False, name='flexible positions')
        plt.style.use('dark_background')
        self.latent_closest = latent_closest
        self.fig = Figure(figsize=(5, 5))
        self.lat_canv = FigureCanvas(self.fig)
        self.axes1 = self.lat_canv.figure.subplots()
        self.axes1.scatter(
            z[:, 0], z[:, 1], c=latent_colors['direction'])
        self.axes1.scatter(
            self.latent_closest[0], self.latent_closest[1], c='r')
        self.lat_im = self.vol_viewer.window.add_dock_widget(self.lat_canv, area='right',
                                                             name='latent space')
        self.vol_viewer.window._qt_window.resizeDocks(
            [self.lat_im], [800], Qt.Horizontal)
        self.rep_menu = widgets.ComboBox(
            label='3d representation', choices=['volume', 'points'])
        self.rep_widget = widgets.Container(widgets=[self.rep_menu])
        self.repw = self.vol_viewer.window.add_dock_widget(self.rep_widget, area='bottom',
                                                           name='3d representation')
        self.rep_col_menu = widgets.ComboBox(label='gaussian colors',
                                             choices=['displacement', 'amplitude', 'width', 'position'])
        self.rep_col_widget = widgets.Container(
            widgets=[self.rep_col_menu])
        self.repc = self.vol_viewer.window.add_dock_widget(self.rep_col_widget, area='bottom',
                                                           name='gaussian colors')
        self.rep_action_menu = widgets.ComboBox(label='action',
                                                choices=['click', 'trajectory', 'particle number',
                                                         'starfile'])
        self.rep_action_widget = widgets.Container(
            widgets=[self.rep_action_menu])
        self.repa = self.vol_viewer.window.add_dock_widget(self.rep_action_widget, area='bottom',
                                                           name='action')
        self.color_menu = widgets.ComboBox(label='color by',
                                           choices=['direction', 'amount', 'location', 'density',
                                                    'log-density', 'index', 'pose', 'shift'])
        self.color_widget = widgets.Container(widgets=[self.color_menu])
        self.colw = self.vol_viewer.window.add_dock_widget(self.color_widget, area='right',
                                                           name='colors')
        self.lat_cols = latent_colors
        self.color_menu.changed.connect(self.coloring_changed)
        self.rep_col_menu.changed.connect(self.coloring_gaussians)
        self.rep_action_menu.changed.connect(self.action_changed)
        self.r = torch.zeros([2, 3])
        self.t = torch.zeros([2, 2])
        self.line = {'color': 'white',
                     'linewidth': 8, 'alpha': 1}

        self.nap_z = torch.zeros(nap_cons_pos.shape[0])
        self.cid = self.fig.canvas.mpl_connect(
            'button_press_event', self.onclick)
        self.lasso = LassoSelector(ax=self.axes1, onselect=self.onSelect,
                                   props=self.line, button=2, useblit=True)
        self.lasso.disconnect_events()
        self.star_nr = 0
        self.indices = indices
        self.poly = PolygonSelector(ax=self.axes1, onselect=self.get_part_nr, props=self.line,
                                    useblit=True)
        self.poly.disconnect_events()
        self.output_directory = output_directory
        self.atomic_model = atomic_model
        self.half_set = half_set

    def run(self):
        napari.run()

    def coloring_changed(self, event):

        selected_label = event

        if selected_label == 'amount':
            self.axes1.clear()
            self.axes1.scatter(
                self.z[:, 0], self.z[:, 1], c=self.lat_cols['amount'], alpha=0.1)
            self.axes1.scatter(
                self.latent_closest[0], self.latent_closest[1], c='r')
            self.lat_canv.draw()

        elif selected_label == 'direction':
            self.axes1.clear()
            self.axes1.scatter(
                self.z[:, 0], self.z[:, 1], c=self.lat_cols['direction'], alpha=0.1)
            self.axes1.scatter(
                self.latent_closest[0], self.latent_closest[1], c='r')
            self.lat_canv.draw()

        elif selected_label == 'location':
            self.axes1.clear()
            self.axes1.scatter(
                self.z[:, 0], self.z[:, 1], c=self.lat_cols['location'], alpha=0.1)
            self.axes1.scatter(
                self.latent_closest[0], self.latent_closest[1], c='r')
            self.lat_canv.draw()
        elif selected_label == 'density':
            h, x, y, p = plt.hist2d(
                self.z[:, 0], self.z[:, 1], bins=(50, 50), cmap='hot')
            ex = [self.axes1.dataLim.x0, self.axes1.dataLim.x1, self.axes1.dataLim.y0,
                  self.axes1.dataLim.y1]
            self.axes1.clear()
            # axes1.hist2d(zz[:,0],zz[:,1],bins = (100,100), cmap = 'hot')
            self.axes1.imshow(
                np.rot90(h), interpolation='gaussian', extent=ex, cmap='hot')
            self.axes1.scatter(
                self.latent_closest[0], self.latent_closest[1], c='r')
            self.lat_canv.draw()
        elif selected_label == 'log-density':
            h, x, y, p = plt.hist2d(
                self.z[:, 0], self.z[:, 1], bins=(50, 50), cmap='hot')
            ex = [self.axes1.dataLim.x0, self.axes1.dataLim.x1, self.axes1.dataLim.y0,
                  self.axes1.dataLim.y1]
            self.axes1.clear()
            # axes1.hist2d(zz[:,0],zz[:,1],bins = (100,100), cmap = 'hot')
            self.axes1.imshow(np.log(1 + np.rot90(h)), interpolation='gaussian', extent=ex,
                              cmap='hot')
            self.axes1.scatter(
                self.latent_closest[0], self.latent_closest[1], c='r')
            self.lat_canv.draw()
        elif selected_label == 'index':
            self.axes1.clear()
            self.axes1.scatter(
                self.z[:, 0], self.z[:, 1], c=self.lat_cols['index'])
            self.axes1.scatter(
                self.latent_closest[0], self.latent_closest[1], c='r')
            self.lat_canv.draw()
        elif selected_label == 'pose':
            self.axes1.clear()
            self.axes1.scatter(
                self.z[:, 0], self.z[:, 1], c=self.lat_cols['pose'])
            self.axes1.scatter(
                self.latent_closest[0], self.latent_closest[1], c='r')
            self.lat_canv.draw()
        elif selected_label == 'shift':
            self.axes1.clear()
            self.axes1.scatter(
                self.z[:, 0], self.z[:, 1], c=self.lat_cols['shift'])
            self.axes1.scatter(
                self.latent_closest[0], self.latent_closest[1], c='r')
            self.lat_canv.draw()

    def coloring_gaussians(self, event):

        selected_label = event

        if selected_label == 'displacement':
            self.point_layer.face_color = 'activity'
            self.point_layer.refresh()
        elif selected_label == 'amplitude':
            self.point_layer.face_color = 'amplitude'
            self.point_layer.refresh()
        elif selected_label == 'width':
            self.point_layer.face_color = 'width'
            self.point_layer.refresh()
        elif selected_label == 'position':
            self.point_layer.face_color = 'position'
            self.point_layer.refresh()

    def action_changed(self, event):
        selected_label = event

        if selected_label == 'click':
            self.cid = self.fig.canvas.mpl_connect(
                'button_press_event', self.onclick)
            self.lasso.disconnect_events()
            self.poly.disconnect_events()
            self.lat_canv.draw_idle()
        elif selected_label == 'trajectory':
            self.fig.canvas.mpl_disconnect(self.cid)
            self.lasso = LassoSelector(ax=self.axes1, onselect=self.onSelect,
                                       props=self.line, button=2, useblit=True)

        elif selected_label == 'particle number':
            self.fig.canvas.mpl_disconnect(self.cid)
            self.lasso.disconnect_events()
            self.lat_canv.draw_idle()
            self.poly = PolygonSelector(ax=self.axes1, onselect=self.get_part_nr, props=self.line,
                                        useblit=True)

        elif selected_label == 'starfile':
            self.fig.canvas.mpl_disconnect(self.cid)
            self.lasso.disconnect_events()
            self.poly = PolygonSelector(ax=self.axes1, onselect=self.save_starfile, props=self.line,
                                        useblit=True)
            self.lat_canv.draw_idle()

    def onclick(self, event):
        global ix, iy
        ix, iy = event.xdata, event.ydata
        if self.decoder.latent_dim > 2:
            inst_coord = torch.tensor(np.array([ix, iy])).float()
            dist = torch.linalg.norm(torch.tensor(
                self.z) - inst_coord.unsqueeze(0), dim=1)
            inst_ind = torch.argmin(dist, 0)
            lat_coord = self.latent_space[inst_ind]
        else:
            inst_coord = torch.tensor(np.array([ix, iy])).float()
            dist = torch.linalg.norm(torch.tensor(
                self.z) - inst_coord.unsqueeze(0), dim=1)
            inst_ind = torch.argmin(dist, 0)
            lat_coord = self.z[inst_ind]

        lat = torch.stack(2 * [lat_coord], 0)

        if self.rep_menu.current_choice == 'volume':
            vol = self.decoder.generate_volume(
                lat.to(self.device), self.r.to(self.device), self.t.to(self.device)).float()
            self.vol_layer.data = (
                vol[0] / torch.max(vol[0])).detach().cpu().numpy()
        elif self.rep_menu.current_choice == 'points':
            proj, proj_im, proj_pos, pos, dis = self.decoder.forward(
                lat.to(self.device), self.r.to(self.device), self.t.to(self.device))
            p = torch.cat([self.nap_zeros.unsqueeze(1), (0.5 + pos[0].detach().cpu()) * self.decoder.box_size],
                          1)
            self.point_layer.data = torch.stack(
                [p[:, 0], p[:, 3], p[:, 2], p[:, 1]], 1)

    def onSelect(self, line):
        path = torch.tensor(np.array(line))
        xval, yval = bezier_curve(path, nTimes=200)
        path = torch.tensor(np.stack([xval, yval], 1))
        n_points, new_points = make_equidistant(xval, yval, 100)
        path = torch.tensor(new_points)
        mu = path[0:2].float()
        vols = []
        poss = []
        ppath = []
        if self.decoder.latent_dim > 2:
            for j in range(path.shape[0]):
                dist = torch.linalg.norm(torch.tensor(
                    self.z) - path[j].unsqueeze(0), dim=1)
                inst_ind = torch.argmin(dist, 0)
                ppath.append(self.latent_space[inst_ind])
            path = torch.stack(ppath, 0)
            path = torch.unique_consecutive(path, dim=0)

        if self.rep_menu.current_choice == 'volume':
            print('Generating movie with', path.shape[0], 'frames')
            for i in tqdm(range(path.shape[0] // 2)):
                mu = path[i:i + 2].float()
                with torch.no_grad():
                    V = self.decoder.generate_volume(
                        mu.to(self.device), self.r.to(self.device), self.t.to(self.device))
                    if self.atomic_model != None:
                        proj, pos, dis = self.decoder.forward(
                            mu.to(self.device), self.r.to(self.device), self.t.to(self.device))
                        # c_pos = inv_half1([mu.to(device)],pos)
                        # points2pdb(args.pdb_series,args.out_dir+'/backwardstate'+ str(i).zfill(3)+'.pdb',c_pos[0]*ang_pix*box_size)
                        # points2pdb(args.pdb_series,args.out_dir+'/forward_state'+ str(i).zfill(3)+'.cif',pos[0]*ang_pix*box_size)
                    # vols.append((V[0]/torch.max(V[0])).float().cpu())
                    # vols.append((V[1]/torch.max(V[1])).float().cpu())
                    vols.append((V[0]).float().cpu())
                    vols.append((V[1]).float().cpu())
            VV = torch.stack(vols, 0)
            VV = VV / torch.max(VV)
            self.vol_layer.data = VV.numpy()
        if self.rep_menu.current_choice == 'points':
            for i in range(path.shape[0] // 2):
                mu = path[i:i + 2].float()
                print(i)
                with torch.no_grad():
                    proj,  pos, dis = self.decoder.forward(
                        mu.to(self.device), self.r.to(self.device), self.t.to(self.device))
                    poss.append(torch.cat([i * torch.ones(pos.shape[1]).unsqueeze(1).to(self.device),
                                           (0.5 + pos[0]) * self.decoder.box_size], 1))
                    poss.append(torch.cat(
                        [(i + 1) * torch.ones(pos.shape[1]).unsqueeze(1).to(self.device),
                         (0.5 + pos[1]) * self.decoder.box_size], 1))

            PP = torch.cat(poss, 0)
            self.point_layer.data = PP.cpu().numpy()

    def get_part_nr(self, verts):
        path = Path(verts)
        new_indices = path.contains_points(self.z)
        num = np.sum(new_indices)
        print('Number of particles in the selected area is:', num)
        self.poly.disconnect_events()
        self.poly = PolygonSelector(ax=self.axes1, onselect=self.get_part_nr, props=self.line,
                                    useblit=True)

    def save_starfile(self, verts):
        path = Path(verts)
        new_indices = path.contains_points(self.z)
        print(self.z[new_indices])
        new_star = self.star.copy()
        ninds = np.where(new_indices == True)
        nindsfull = self.indices[ninds[0]]
        new_star['particles'] = self.star['particles'].loc[list(nindsfull)]
        print('created star file with ', len(ninds[0]), 'particles')
        starfile.write(new_star, self.output_directory + 'subset_' +
                       str(self.star_nr) + '_half' + str(self.half_set) + '.star')
        np.savez(
            self.output_directory + 'subset_' +
            str(self.star_nr) + '_indices_' + str(self.half_set) + '.npz',
            nindsfull)
        self.star_nr += 1
        self.poly.disconnect_events()
        self.poly = PolygonSelector(ax=self.axes1, onselect=self.save_starfile, props=self.line,
                                    useblit=True)