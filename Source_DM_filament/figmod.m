function [ hFig,hAxe ] = figmod( hFig, hAxe, pos_x, pos_y, fig_width,fig_height,margin_vert_u,margin_vert_d,margin_horz_l,margin_horz_r )
% modified 21/11/04

fontname = 'Microsoft Sans Serif';
set(0,'defaultaxesfontname',fontname);
set(0,'defaulttextfontname',fontname);
fontsize = 10; % pt
set(0,'defaultaxesfontsize',fontsize);
set(0,'defaulttextfontsize',fontsize);
% set(0,'fixedwidthfontname','times');

set(hFig,'renderer','painters');
set(hFig,'units','inches');
set(hFig,'position',[pos_x pos_y fig_width fig_height]);
set(hFig,'PaperUnits','inches');
set(hFig,'PaperSize', [fig_width fig_height]);
set(hFig,'PaperPositionMode', 'manual');
set(hFig,'PaperPosition',[pos_x pos_y fig_width fig_height]);
set(hFig,'Color','w');

set(hAxe,'activepositionproperty','outerposition');
set(hAxe,'units','inches');
ax_pos = get(hAxe,'position');
ax_pos(4) = fig_height-margin_vert_u-margin_vert_d;
ax_pos(2) = fig_height-(margin_vert_u+ax_pos(4));
ax_pos(3) = fig_width-margin_horz_l-margin_horz_r;
ax_pos(1) = margin_horz_l;
set(hAxe,'position',ax_pos);

end